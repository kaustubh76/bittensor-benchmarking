#!/usr/bin/env python3
"""Run the spend operator for one registered business.

    uv run python scripts/operator_run.py --business acr-fleet
    uv run python scripts/operator_run.py --business acr-fleet --live   # pays
    uv run python scripts/operator_run.py --business acme --entitlements bills.json

THE SECOND SOURCE. `--entitlements` reads bills that did NOT come through our
paywall — a recurring vendor's invoice for fifty seats, a contractor's milestone
— each one paired with an independent count of what was actually used. That is
Prior Art #06 applied where it is worth the most: the agoranomoi kept the
standard measures because a seller who supplies both the goods and the measuring
cup will eventually supply a smaller cup, and a SaaS invoice arrives already
totalled.

A business with NO `PolicyWallet` is a first-class case here, not a degraded
one: the operator prices, meters and reports, and cannot spend. The registry
calls that "what an evaluation looks like", and it is the whole of what a new
customer has to trust us with — a usage export, and no keys.

WHAT AN OBLIGATION IS HERE, and why it is not invented. The fleet's own
settlement archive says which machine services it consumed and how much of each.
The marketplace catalog says what each seller is asking for those same services
right now. One obligation per (seller, resource) the fleet actually bought from:
*this seller wants this price for a service we really consume, and we have our
own count of how much we took*. Nothing is synthesised — the vendor, the
resource, the quantity and the price all come from records that existed before
this script ran.

DRY RUN IS THE DEFAULT, and with no PolicyWallet configured it is the only mode:
the operator prices and meters and writes down what it would do. `--live` is
refused unless the business has a wallet, because a "live" run that silently
could not pay would be a dry run wearing the wrong label.

The decisions this writes are what `/spend` reads. They are also the honest
Tameion delta for this business: its settlements predate the window, every
decision here does not.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time
from collections import defaultdict

from acr_core import get_settings
from index_api import entitlements as entitlements_register
from index_api.businesses import resolve
from index_api.marketplace import build_catalog
from index_api.operator import (
    ESCALATE,
    PAY,
    obligations_for,
    obligations_from_entitlements,
    resource_path,
    run_obligation,
    settled_refs_from,
    settled_through,
)

#: The seller's committed settlement archive.
RECEIPTS_ARCHIVE = pathlib.Path(__file__).resolve().parents[1] / "services/index_api/index_api/receipts_live.jsonl"


def _receipts(settings) -> list[dict]:
    """The business's own settlement history, ON THIS RUN'S CHAIN ONLY.

    The archive holds every chain's settlements (on 2026-10-16: 120 rows, all
    `eip155:5042002`). Read unfiltered, a mainnet run would bill testnet activity
    and pay it with mainnet USDC. Stricter than the press's rehydration, which
    keeps a row with no `network`: paying is irreversible, so "unknown" is not
    good enough here.
    """
    import json

    if not RECEIPTS_ARCHIVE.exists():
        return []
    here = settings.caip2()
    rows = []
    for line in RECEIPTS_ARCHIVE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if str(row.get("network") or "") == here:
            rows.append(row)
    return rows


def _rpc_host(url: str) -> str:
    """The endpoint's host, never its path.

    `https://rpc.testnet.arc-node.thecanteenapp.com/v1/<key>` identifies us by a
    secret in its path, so the only safe thing to print is the hostname.
    """
    from urllib.parse import urlparse

    try:
        return urlparse(url or "").hostname or "(none)"
    except ValueError:
        return "(unparseable)"


def _path_of(resource: str) -> str:
    """Kept as a name this file already uses; the rule lives in `operator`."""
    return resource_path(resource)


#: The builder moved into the package, because the press now needs it too: an
#: unattended operator cannot import from `scripts/`, and two copies of the rule
#: that decides what a business owes is the one duplication this product cannot
#: afford. Kept as a name this file already uses.
_obligations = obligations_for


def _read_entitlements(path: str) -> list[dict] | None:
    """Bills a vendor sent us, as JSON. A list, or an object with ``bills``.

    Returns None on anything unreadable, so `main` can refuse with a sentence
    instead of a traceback — this is the one input that comes from outside the
    repo, typed or exported by somebody else, and the first thing a new customer
    ever hands over.
    """
    import json

    try:
        raw = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"no such file: {path}", file=sys.stderr)
        return None
    except ValueError as exc:
        print(f"{path} is not valid JSON ({exc})", file=sys.stderr)
        return None
    rows = raw.get("bills") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        print(
            f"{path} should be a JSON list of bills, or an object with a `bills` list",
            file=sys.stderr,
        )
        return None
    return [r for r in rows if isinstance(r, dict)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--business", required=True, help="registry slug or treasury address")
    ap.add_argument("--live", action="store_true", help="actually pay what clears policy")
    ap.add_argument("--limit", type=int, default=0, help="stop after N obligations")
    ap.add_argument(
        "--entitlements", default="",
        help="a JSON file of bills that did not come through our paywall: each row "
             "payee, resource, billed_usdc, billed_quantity, used_quantity, period_*",
    )
    args = ap.parse_args()

    s = get_settings()
    b = resolve(args.business)
    if b is None:
        print(f"no business registered as {args.business!r}", file=sys.stderr)
        return 2
    from acr_core.mainnet_guard import MAINNET_CHAIN_ID

    here = "mainnet" if int(s.arc_chain_id) == MAINNET_CHAIN_ID else "testnet"
    if getattr(b, "chain", "testnet") != here:
        print(
            f"{b.slug} is a {b.chain} business, but this run is on chain {s.arc_chain_id} ({here}). "
            "Refusing: its bills would be judged against the wrong chain's receipts and paid with "
            "that chain's USDC. Set ACR_ARC_CHAIN_ID and ACR_ARC_RPC_URL for its chain.",
            file=sys.stderr,
        )
        return 2

    policy = None
    if b.policy_wallet:
        from acr_oracle_client.policy import PolicyClient

        policy = PolicyClient(wallet_address=b.policy_wallet)
    elif args.live:
        print(
            f"{b.slug} has no PolicyWallet, so --live could not pay anything. "
            "Refusing rather than running a dry run under the wrong name.",
            file=sys.stderr,
        )
        return 2

    # What has already been paid, so check 1 has something to check against. It
    # never did: `decide` has taken `settled_refs` since it was written and this
    # script, its only real caller, never passed one — so a retried bill was
    # paid twice and the log would have shown two clean payments.
    from index_api.statement import read_decisions

    # Read ONCE, used twice: which references have settled, and where each
    # pair's last paid period ended. Two questions about the same record, so
    # asking it twice would invite the two answers to disagree.
    #
    # BEFORE the obligations are built, necessarily — the window each bill
    # covers starts where the last paid one ended, so this is an input to
    # `_obligations` and not a thing to look up afterwards.
    decisions = read_decisions(business=b.slug)
    already = settled_refs_from(decisions)
    paid_through = settled_through(decisions)

    receipts = _receipts(s)
    catalog = build_catalog(s.x402_resource_base or "http://127.0.0.1:8000", s)

    # EACH OBLIGATION CARRIES THE COUNT THAT JUDGES IT, or None where we have
    # none. The tape-fed ones let `run_obligation` meter them from the receipts
    # it is handed; an entitlement arrives with its usage already measured by
    # somebody else, and `metered_quantity` is the seam that carries it in.
    # Pairing them here rather than tracking two lists is what makes it
    # impossible to run a bill past the meter by accident — the meter check is
    # skipped by a `None` guard, so its absence leaves no trace in the record.
    # BOTH SOURCES, ADDED — the way `operator_keeper` does it (`pairs +=`).
    #
    # This used to be an either/or: naming `--entitlements` replaced the tape
    # entirely, and omitting it hid the register. So the autonomous path and the
    # CLI a person runs to check it disagreed about what was owed, and the
    # register — which only the keeper read — was invisible to the one command
    # anybody would use to verify it. A business has both a settlement tape and
    # vendor invoices; this docstring calls the second one "THE SECOND SOURCE",
    # which is additive by its own wording.
    pairs = [(ob, None) for ob in _obligations(b, receipts, catalog, paid_through)]
    source = f"{len(receipts)} settlement(s) in the archive"

    if args.entitlements:
        rows = _read_entitlements(args.entitlements)
        if rows is None:
            return 2
        where = args.entitlements
    else:
        # The committed register, keyed per business, exactly as the keeper reads
        # it. Absent or empty is a normal state, not an error.
        rows = list(entitlements_register.for_business(b.slug))
        where = "the committed register"
    if rows:
        pairs += obligations_from_entitlements(b, rows, paid_through)
        source += f" + {len(rows)} bill(s) from {where}"
    if args.limit:
        pairs = pairs[: args.limit]
    obligations = [ob for ob, _ in pairs]

    # WHICH ENDPOINT THIS RAN THROUGH. Nothing in this repo recorded that, and on
    # testnet it decides whether the work counts at all: Canteen counts traction
    # through the RPC they issue, so a run against the public endpoint is work
    # that happened and was not seen. The HOST only — the Canteen URL carries a
    # key in its path, and a key printed to a terminal is a key in a transcript.
    print(f"rpc        : {_rpc_host(s.arc_rpc_url)}")
    print(f"business   : {b.slug} ({b.public_name}) on {b.chain}")
    print(f"bills from : {source}")
    print(f"settled    : {len(already)} reference(s) already paid")
    print(f"paid thru  : {len(paid_through)} (seller, resource) pair(s) settled to a point in time")
    print(f"obligations: {len(obligations)}")
    # NOTHING NEW AND NOTHING AT ALL ARE DIFFERENT STATES, and on a schedule the
    # first is the healthy majority case. A bare "obligations: 0" reads as a
    # broken run every time the operator has simply caught up, which is how an
    # operator learns to ignore its own output.
    if not obligations:
        print(
            "           → nothing has settled since the last bill we paid"
            if paid_through
            else "           → no settlement on the tape is payable by this treasury"
        )
    print(f"mode       : {'LIVE' if args.live else 'dry run'}\n")

    # ONE CASH PICTURE FOR THE PASS, not one per bill. `due_usdc` is a sum over
    # the obligations in hand, so it has to be computed where the set is. And
    # re-reading the balance per bill would be N chain calls saying the same
    # thing, while letting two decisions in one pass be judged against two
    # different balances — which no reviewer could reconstruct afterwards from
    # a log that records one number per row.
    from index_api.liquidity import assess as assess_liquidity

    liq = assess_liquidity(
        policy.balance_usdc() if policy is not None else None,
        [ob for ob, _ in pairs],
        time.time(),
    )
    print(
        f"cash       : {'not measured' if liq.held_usdc is None else f'{liq.held_usdc:g} USDC held'}"
        f" · {liq.due_usdc:g} dated in {int(liq.horizon_s / 86_400)}d"
        f"{f' · {liq.undated} undated' if liq.undated else ''}"
    )

    tally: dict[str, int] = defaultdict(int)
    for ob, metered in pairs:
        d = run_obligation(
            ob,
            receipts=receipts,
            catalog=catalog,
            policy=policy,
            metered_quantity=metered,
            liquidity=liq,
            settled_refs=already,
            # The same window the bill was built from. `since` defaulted to 0.0
            # and was never passed, so the meter counted from the epoch — which
            # was accidentally right while the bill was all-time too, and
            # becomes a false discrepancy on every row the moment it is not.
            since=ob.period_start,
            dry_run=not args.live,
        )
        # Within one run too: two obligations resolving to one reference is the
        # retry shape, and the second must not be paid because the first just
        # was. Only a real payment joins the set, for the same reason
        # `settled_refs_from` ignores dry runs.
        if d.intent == PAY and d.paid_usdc > 0:
            already.add(ob.obligation_id)
            if ob.invoice_ref:
                already.add(ob.invoice_ref)
        tally[d.intent] += 1
        mark = {PAY: "pay", ESCALATE: "ask", "reroute": "reroute",
                "hold": "hold", "refuse": "refuse"}.get(d.intent, d.intent)
        name = ob.resource.rsplit("/", 1)[-1]
        print(f"  [{mark:>7}] {d.billed_usdc:>10.6f} {name:<24} {d.rule}")

    print("\n" + " · ".join(f"{k} {v}" for k, v in sorted(tally.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
