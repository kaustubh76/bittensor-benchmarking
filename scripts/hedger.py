#!/usr/bin/env python
"""The autonomous hedger — an agent that buys the index, then acts on it.

This is the loop the whole project exists to make possible, run end to end by
one wallet with no human in it:

    1. PAY   a real x402 nanopayment for the latest ACR-INF print
    2. READ  its own on-chain position on the futures venue
    3. DECIDE how far it is from the hedge its mandate asks for
    4. TRADE the difference on ACRFutures — a real fill on the public tape

Every other agent in this repo does one leg. The buyer pays but never acts on
what it bought; the heartbeat trades but never pays for the data it trades on.
Neither is an economic decision. This one is: the print it purchases is the
input to the position it takes — and that join belongs to the venue, not to
this script. ``ACRFutures.trade`` fills at ``oracle.latestValue(indexId)``, so
the number this agent paid for IS the number it was filled at, whatever this
file happens to write down.

Which matters, because the log below is a DIAGNOSTIC and not evidence. It
lands in ``data/``, which is gitignored and dockerignored, so it never leaves
the machine that ran it — and ``print_tx`` was null on all seven of its first
live lines while the payments themselves succeeded (see ``buy_the_print``).
The claim a stranger can check is ``GET /hedger``: the Gateway settlements
this wallet made, beside the position they bought, one from the public
receipts tape and one from the chain. Write this log for yourself; point other
people at that.

## Why a Circle AGENT wallet, and not the ones the venue uses

The maker, taker and press are Circle **developer-controlled** wallets because
they run from cron and an OTP session would expire on them (docs/WALLETS.md).
This agent is the opposite case — it is the thing Circle's agent wallet was
built for: "a programmatic USDC wallet for AI agents … to pay for x402
services". So it signs through the Circle CLI and holds no exportable key:

    circle services pay <url> --address <agent> --chain ARC-TESTNET
    circle wallet execute "trade(uint256,int256)" <series> <qty> …

That also settles a question this repo had answered too broadly. The runbook
says an x402 buyer "needs a raw, exportable EOA key" because the `exact` scheme
is EIP-3009 and the facilitator `ecrecover`s it. True when OUR code signs — but
when the CLI pays, Circle signs through the agent wallet's backing EOA and
nothing is exported. Both designs are valid; this is the one with no key.

## The guardrails, and an honest note about them

Circle agent wallets support platform-enforced spending policies — per-tx,
daily, weekly and monthly caps the CLI itself refuses to exceed. They are
**mainnet-only**; the testnet chains this runs on reject them. So every limit
below is enforced by this script, which means it is only as good as this script.
That is a weaker claim than "the platform will not let it overspend" and it is
written here so nobody upgrades it in the retelling.

What is enforced:

  * a per-run USDC spend cap, passed to the CLI as `--max-amount` too, so the
    payment layer refuses independently of our arithmetic;
  * a hard position cap in contracts;
  * a gas floor — on Arc USDC *is* gas, so a wallet spent to zero cannot exit;
  * a kill switch checked before every action;
  * sizing through the desk's own ``feasible_qty``, the same clamp a reader
    gets, so the agent cannot ask for a fill the contract would revert.

A breach REFUSES rather than silently clamping. A guardrail that quietly
shrinks your order teaches you nothing; one that stops and says why is a fact
you can act on.

    uv run python scripts/hedger.py --once            # == make hedger
    HEDGER_DRY_RUN=1 uv run python scripts/hedger.py  # decide, never spend
    HEDGER_TARGET=-2 …                                # the mandate, in contracts

Exit codes: 0 = the loop ran and every decision was honoured (including a
deliberate refusal); 1 = it could not run (no wallet, no venue, chain unreadable).
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field

from acr_core import get_settings
from acr_oracle_client import FuturesClient, OracleClient, select_series_for_index
from acr_oracle_client.futures import _rpc_retry, collateral_or_none

# The desk's own margin arithmetic, reused rather than re-derived — if the agent
# and the desk ever disagreed about a safe size, one of them would be lying to
# somebody about their money.
from index_api.desk import feasible_qty

AGENT_ADDRESS = os.environ.get("ACR_HEDGER_ADDRESS", "").strip()
CHAIN = os.environ.get("ACR_HEDGER_CHAIN", "ARC-TESTNET")
# Arc MAINNET. The Arc testnet host this defaulted to (`acr-api-1fto`) has been
# suspended since 2026-09-15 and answers 503 — a default nothing in the repo
# shows you using is exactly how the press lost 48.9 hours of prints.
API = os.environ.get("ACR_API_URL", "https://acr-api-mainnet.onrender.com").rstrip("/")
INDEX = os.environ.get("HEDGER_INDEX", "ACR-INF")

#: The mandate: the position, in contracts, this agent is trying to hold. A
#: compute BUYER is short the rate it pays, so it hedges by going long the
#: future — a negative target would be a seller's mandate.
#: The mandate. Matches the DEPLOYED value (services/index_api/hedger.py, and
#: HEDGER_TARGET on Render) on purpose: this defaulted to 1.0 while the live
#: service advertised 2.0, so `make hedger` with only ACR_HEDGER_ADDRESS
#: exported would have SOLD 0.82 — the exact opposite of the mandate the site
#: shows a judge. Two files, two defaults, one agent.
TARGET = float(os.environ.get("HEDGER_TARGET", "2.5"))
#: Do not trade for less than this; a dust fill costs more in gas than it hedges.
MIN_TRADE = float(os.environ.get("HEDGER_MIN_TRADE", "0.25"))

# --- the guardrails ---------------------------------------------------------
SPEND_CAP_USDC = float(os.environ.get("HEDGER_SPEND_CAP_USDC", "0.005"))
POSITION_CAP = float(os.environ.get("HEDGER_POSITION_CAP", "3.0"))
GAS_FLOOR_USDC = float(os.environ.get("HEDGER_GAS_FLOOR_USDC", "1.0"))
COLLATERAL_USDC = float(os.environ.get("HEDGER_COLLATERAL_USDC", "2.0"))
#: The most one tick may add to its own margin, and the most it may ever hold.
#: An agent that can post collateral is an agent that can spend, so the ceiling
#: is here rather than in the caller's head.
TOPUP_MAX_USDC = float(os.environ.get("HEDGER_TOPUP_MAX_USDC", "0.50"))
COLLATERAL_CAP_USDC = float(os.environ.get("HEDGER_COLLATERAL_CAP_USDC", "3.0"))
KILL_SWITCH = os.environ.get("HEDGER_STOP", "") not in ("", "0", "false")
DRY_RUN = os.environ.get("HEDGER_DRY_RUN", "") not in ("", "0", "false")

INTERVAL_S = float(os.environ.get("HEDGER_INTERVAL", "180"))
MAX_TICKS = int(os.environ.get("HEDGER_MAX_TICKS", "3"))
CLI_TIMEOUT_S = float(os.environ.get("HEDGER_CLI_TIMEOUT_S", "180"))
LOG_PATH = os.environ.get("HEDGER_LOG_PATH", "data/hedger_decisions.jsonl")

USDC = os.environ.get("ACR_USDC_ADDRESS", "0x3600000000000000000000000000000000000000")


@dataclass
class Decision:
    """One tick, with the inputs that justified it.

    Logged whole and deliberately: "the agent traded" is an assertion, while
    "the agent held -0.75 against a target of 1.0 at a mark of 0.4953, so it
    bought 1.0" is a claim a judge can check against the chain.
    """

    at: float
    mark: float | None = None
    position: float | None = None
    target: float = TARGET
    gap: float | None = None
    intent: str = "hold"
    qty: float = 0.0
    spent_usdc: float = 0.0
    refused: str | None = None
    print_tx: str | None = None
    trade_tx: str | None = None
    notes: list[str] = field(default_factory=list)


def log_decision(d: Decision) -> None:
    line = json.dumps(asdict(d))
    print(f"  · {line}")
    try:
        path = LOG_PATH
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001 — a log that cannot write must not stop the agent
        pass


def circle(args: list[str], *, timeout: float = CLI_TIMEOUT_S) -> tuple[int, dict | None, str]:
    """Run the Circle CLI and parse its JSON. Returns ``(code, data, raw)``.

    Shelling out is the point rather than a shortcut: it means the agent signs
    with Circle's own agent tooling and never touches a key. The CLI is invoked
    through ``npx`` so a machine without a global install still works.
    """
    cmd = ["npx", "-y", "@circle-fin/cli@latest", *args, "--output", "json"]
    # --experimental-global-webcrypto is REQUIRED for `services pay`. Without it
    # the CLI dies inside Gateway batched-payment signature creation with
    # "ReferenceError: crypto is not defined" — and it says "Could not sign
    # payment authorization", which reads like a wallet or protocol problem
    # rather than a missing Node global. Measured on Node v26, where `crypto`
    # IS a global at the top level, so something in the CLI's signing path runs
    # without it. `circle wallet execute` is unaffected; only the payment leg.
    env = {
        **os.environ,
        "CIRCLE_ACCEPT_TERMS": "1",
        "NODE_OPTIONS": (
            os.environ.get("NODE_OPTIONS", "") + " --experimental-global-webcrypto"
        ).strip(),
    }
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except Exception as exc:  # noqa: BLE001 — a verdict beats a traceback
        return 1, None, f"{type(exc).__name__}: {exc}"
    raw = (p.stdout or "") + (p.stderr or "")
    # npm chatters on stderr; find the JSON rather than assuming it stands alone.
    for start in (raw.find("{"), raw.find("[")):
        if start >= 0:
            try:
                return p.returncode, json.loads(raw[start:]), raw
            except Exception:
                continue
    return p.returncode, None, raw


def margin_bound(
    want: float, t_cap: float, position: float, m_cap: float, maker_inv: float
) -> bool:
    """True when OUR OWN margin is what blocks the trade, not the book's depth.

    ``feasible_qty`` returns ``min(t_cap − position, m_cap + maker_inv)`` for a
    buy and ``min(t_cap + position, m_cap − maker_inv)`` for a sell. Which term
    binds decides what an agent should DO about it, and the two answers are
    opposite:

    * the maker's term binds → the BOOK is full. Posting collateral spends
      money and still cannot trade. Refuse, and say so accurately.
    * our own term binds → we are margin-bound, and we can fix it. An agent
      with a mandate it can reach and does not is not much of an agent.

    Getting this backwards is the expensive direction: it buys margin that
    changes nothing. So the test is explicit rather than inferred from
    ``room <= 0``, which cannot tell you which side ran out.
    """
    mine, theirs = (
        (t_cap - position, m_cap + maker_inv)
        if want > 0
        else (t_cap + position, m_cap - maker_inv)
    )
    return mine < theirs


def topup_for(
    want: float, mine_now: float, per_contract: float, safety: float,
    posted: float, topup_max: float, cap: float,
) -> float:
    """USDC to add so our own cap covers ``want`` — bounded twice, rounded up.

    Returns 0.0 when nothing is needed or nothing is allowed. `per_contract` is
    ``mark × multiplier × MARGIN_BPS/1e4``: the same arithmetic the desk margins
    with, so the number we buy is the number that clears.
    """
    if per_contract <= 0 or safety <= 0:
        return 0.0
    need_cap = abs(want) + (mine_now if want > 0 else -mine_now)
    need_collateral = need_cap * per_contract / safety
    short = need_collateral - posted
    if short <= 0:
        return 0.0
    allowed = min(topup_max, max(0.0, cap - posted))
    return round(min(short + 0.01, allowed) + 0.004, 2)


def _fmt_qty(q: float) -> str:
    """Contracts as the contract wants them: WAD-scaled integer, as a string.

    A float would reach the CLI in scientific notation for small sizes and be
    ABI-encoded as something nobody intended.

    KNOWN LIMIT, measured 2026-08-08 on ARC-TESTNET: a NEGATIVE value does not
    survive ``circle wallet execute``. ``+1e16`` estimates and returns a fee;
    ``-1e16`` fails with ``400 Fails to perform transaction estimation``, and so
    do both two's-complement spellings and a ``--`` separator. It is not a
    revert — the identical call succeeds under ``eth_call`` against the venue,
    and an oversized POSITIVE quantity returns the different error ``Estimate
    fee execution reverted``, which is what a revert actually looks like. The
    transaction cannot be BUILT, so this agent can open and increase a position
    through the agent wallet but cannot reduce one. See skills/acr-hedge.
    """
    return str(int(round(q * 10**18)))


#: Every key a `circle services pay` response has plausibly carried the
#: settlement reference under. Ordered by specificity, and `id` is last on
#: purpose — it is the key most likely to belong to some wrapper object that is
#: not the settlement at all.
REF_KEYS = (
    # `receipt` is FIRST because it is the one the CLI actually uses: a live run
    # on 2026-08-08 reported `data.payment = {amount, chain, receipt, scheme,
    # seller}`. That was never a guess — the diagnostic below printed the
    # response's shape after failing to find a reference, and this key is what
    # it named. The rest stay as plausible alternates across CLI versions.
    "receipt",
    "settlementRef", "settlement_ref", "settlementReference",
    "transactionId", "transaction_id", "txRef", "tx_ref", "txHash",
    "paymentId", "payment_id", "batchId", "batch_id",
    "referenceId", "reference_id", "id",
)


def _find_ref(node: object, depth: int = 0) -> str | None:
    """The first plausible settlement reference anywhere in a pay response.

    Recursive rather than top-level, because the failure this replaces was not
    "we guessed the wrong name" so much as "we guessed the wrong DEPTH": the
    caller already unwraps one ``data`` envelope, and a reference one level
    below that was invisible to three ``.get`` calls. Depth-bounded so a large
    payload — the purchased print rides along in the same response — cannot turn
    a log line into a tree walk.

    Values are type-checked before they are believed: a bool under ``id`` is a
    flag and a dict under ``transactionId`` is some other object, and writing
    either into ``print_tx`` would replace a null with something worse — a field
    that looks populated and resolves to nothing.
    """
    if depth > 4 or not isinstance(node, dict):
        return None
    for k in REF_KEYS:
        v = node.get(k)
        if isinstance(v, (str, int)) and not isinstance(v, bool) and str(v).strip():
            return str(v).strip()
    for v in node.values():
        if isinstance(v, dict):
            if hit := _find_ref(v, depth + 1):
                return hit
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict) and (hit := _find_ref(item, depth + 1)):
                    return hit
    return None


def _settlement_ref(raw: str | None) -> str | None:
    """The Gateway batch UUID, unwrapped from whatever the CLI handed back.

    ``data.payment.receipt`` is the x402 ``X-PAYMENT-RESPONSE`` header verbatim:
    base64 over ``{"success", "transaction", "network", "payer"}``. Storing that
    blob would be worse than storing nothing — it LOOKS like a reference and
    matches no receipt anyone can look up. The ``transaction`` inside it is the
    real one: measured on 2026-08-08, the decoded value
    ``b0c1be36-6283-4660-af3e-2ad0a8bdfe33`` is byte-for-byte the ``tx_ref`` the
    seller's own ``/marketplace/receipts`` published for that payment. So the
    log and the public tape can finally name the same thing.
    """
    if not raw:
        return None
    ref = str(raw).strip()
    try:
        decoded = json.loads(base64.b64decode(ref, validate=True))
    except Exception:
        return ref  # already a plain reference, or not base64 at all
    if isinstance(decoded, dict):
        for k in ("transaction", "transactionId", "id"):
            v = decoded.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ref


def buy_the_print(spent_so_far: float, d: Decision) -> float:
    """Pay for one print through the agent wallet. Returns USDC spent this call."""
    price_cap = min(SPEND_CAP_USDC - spent_so_far, SPEND_CAP_USDC)
    if price_cap <= 0:
        d.notes.append("spend cap reached — did not buy a print")
        return 0.0
    url = f"{API}/prints/{INDEX}"
    args = ["services", "pay", url, "--address", AGENT_ADDRESS, "--chain", CHAIN,
            "--max-amount", f"{price_cap:.6f}"]
    if DRY_RUN:
        args.append("--estimate")
    code, data, raw = circle(args)
    if code != 0 or data is None:
        d.notes.append(f"payment failed: {raw.strip()[:160]}")
        return 0.0
    body = data.get("data", data)
    if DRY_RUN:
        d.notes.append(f"dry run — would pay {body.get('price', '?')}")
        return 0.0
    # Search the whole response, not three keys at one depth — and search the
    # envelope too, because `body` above already discarded one level and the
    # reference may have been in the level it discarded.
    d.print_tx = _settlement_ref(_find_ref(body) or _find_ref(data))
    # Trust the seller's own price, not our guess at it.
    paid = float(body.get("amountPaid") or body.get("amount") or 0.0001)
    d.notes.append(f"paid {paid} USDC for {INDEX}")
    if not d.print_tx:
        # The diagnostic that was missing. `print_tx` was null on all seven of
        # this log's first live lines — including three whose own note says the
        # payment SUCCEEDED — and nothing recorded what the CLI had actually
        # returned, so every run rediscovered nothing and wrote another silent
        # null. Record the SHAPE once, so the next paid run either finds the
        # reference or names the key it should have looked under.
        #
        # Keys only, never values: a pay response carries the purchased print
        # and a payment authorization, and a decision log is the wrong place for
        # either. Two levels, because "which envelope was it in" is half the
        # question and printing only the inner one already lost that answer.
        # Nested one level, because the first live run of this diagnostic came
        # back `keys ['payment', 'response']` — which proved the reference was
        # not at the top and still did not say where it WAS. A shape report that
        # stops at the outermost layer only moves the mystery inward.
        def _shape(node: object, depth: int = 0) -> object:
            if not isinstance(node, dict) or depth > 2:
                return type(node).__name__
            return {k: _shape(v, depth + 1) for k, v in sorted(node.items())}

        d.notes.append(
            f"paid, but found no settlement ref: shape {_shape(data)}"
        )
    return paid


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    s = get_settings()

    if not AGENT_ADDRESS:
        print("set ACR_HEDGER_ADDRESS to the Circle agent wallet's address")
        sys.exit(1)
    if not s.futures_address:
        print("set ACR_FUTURES_ADDRESS")
        sys.exit(1)

    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(s.arc_rpc_url, request_kwargs={"timeout": 25}))
    if not w3.is_connected():
        print("Arc RPC unreachable")
        sys.exit(1)
    me = Web3.to_checksum_address(AGENT_ADDRESS)  # Circle returns lowercase
    fc = FuturesClient(rpc_url=s.arc_rpc_url, futures_address=s.futures_address)
    oracle = OracleClient(rpc_url=s.arc_rpc_url, oracle_address=s.oracle_address or None)

    print(f"hedger  agent {me} (Circle agent wallet, no exportable key)")
    print(f"        venue {s.futures_address}  index {INDEX}  target {TARGET:+.2f}")
    print(f"        caps: spend {SPEND_CAP_USDC} USDC · position {POSITION_CAP} · "
          f"gas floor {GAS_FLOOR_USDC}"
          + ("  [DRY RUN]" if DRY_RUN else ""))
    print("        note: agent-wallet spending POLICY is mainnet-only, so these "
          "caps are enforced here, not by the platform\n")

    spent = 0.0
    once = "--once" in sys.argv
    ticks = 1 if once else MAX_TICKS

    for _tick in range(1, ticks + 1):
        d = Decision(at=time.time())

        if KILL_SWITCH or os.environ.get("HEDGER_STOP", "") not in ("", "0", "false"):
            d.refused = "kill switch set (HEDGER_STOP)"
            log_decision(d)
            break

        gas = _rpc_retry(w3.eth.get_balance, me) / 1e18
        if gas < GAS_FLOOR_USDC:
            d.refused = f"gas {gas:.3f} < floor {GAS_FLOOR_USDC} — refusing to trade a wallet that could not exit"
            log_decision(d)
            break

        # 1) Buy the print. This is the input to everything below it.
        spent += buy_the_print(spent, d)
        d.spent_usdc = round(spent, 6)

        # 2) Read the venue and our own position.
        series = select_series_for_index(fc.read_all_series(), INDEX)
        if not series or series.get("settled"):
            d.refused = "no live series to hedge on"
            log_decision(d)
            break
        sid, mult = series["series_id"], series["multiplier"]
        mark = oracle.read_latest(INDEX)
        d.mark = float(mark["value"]) if mark else None
        if not d.mark:
            d.refused = "no live mark — refusing to size a trade against a guess"
            log_decision(d)
            break

        pos = fc.position_of(sid, me) or {"contracts": 0.0}
        d.position = float(pos.get("contracts", 0.0))
        mine = collateral_or_none(fc, sid, me)
        if mine is None:
            d.refused = "the chain would not say what collateral I hold — a throttled read is not an empty account"
            log_decision(d)
            break

        # 3) Decide. The gap between the mandate and reality IS the trade.
        d.gap = TARGET - d.position
        if abs(d.gap) < MIN_TRADE:
            d.intent = "hold"
            d.notes.append(f"within {MIN_TRADE} of target — nothing worth paying gas for")
            log_decision(d)
            if once:
                break
            time.sleep(INTERVAL_S)
            continue

        # A cap REFUSES; it does not quietly shrink the order.
        if abs(d.position + d.gap) > POSITION_CAP:
            d.refused = (f"target {TARGET:+.2f} would breach the position cap "
                         f"{POSITION_CAP} — refusing rather than clamping")
            log_decision(d)
            break

        # Provision BEFORE sizing. `feasible_qty` caps the trade at what this
        # wallet's own collateral can margin, so an unprovisioned agent is
        # sized to exactly zero and refuses — correct arithmetic, wrong order.
        # Collateral is per series, so this fires again after every roll.
        if mine <= 0:
            if DRY_RUN:
                d.notes.append(
                    f"no collateral on series {sid} — would post {COLLATERAL_USDC} USDC "
                    "before sizing"
                )
            else:
                for sig, params in (
                    ("approve(address,uint256)", [s.futures_address, str(2**256 - 1)]),
                    ("postCollateral(uint256,uint256)",
                     [str(sid), str(int(COLLATERAL_USDC * 1_000_000))]),
                ):
                    contract = USDC if sig.startswith("approve") else s.futures_address
                    code, _, raw = circle(["wallet", "execute", sig, *params,
                                           "--contract", contract, "--address", AGENT_ADDRESS,
                                           "--chain", CHAIN])
                    if code != 0:
                        d.refused = f"{sig.split('(')[0]} failed: {raw.strip()[:160]}"
                        log_decision(d)
                        sys.exit(1)
                    d.notes.append(f"{sig.split('(')[0]} ok")
                    time.sleep(3)
                reread = collateral_or_none(fc, sid, me)
                if reread is None or reread <= 0:
                    d.refused = "posted collateral but the venue still reads zero — refusing to trade on a guess"
                    log_decision(d)
                    break
                mine = reread
                d.notes.append(f"collateral now {mine:.2f} USDC")

        maker = Web3.to_checksum_address(series["maker"])
        maker_coll = collateral_or_none(fc, sid, maker)
        if maker_coll is None:
            d.refused = "could not read the maker's collateral — refusing to size against a guess"
            log_decision(d)
            break
        margin_bps = 2000
        try:
            margin_bps = int(_rpc_retry(
                w3.eth.contract(
                    address=Web3.to_checksum_address(s.futures_address),
                    abi=[{"type": "function", "name": "MARGIN_BPS", "stateMutability": "view",
                          "inputs": [], "outputs": [{"name": "", "type": "uint256"}]}],
                ).functions.MARGIN_BPS().call
            ))
        except Exception:
            d.notes.append("MARGIN_BPS unreadable — using the deployed default 2000")

        max_buy, max_sell = feasible_qty(
            d.mark, mult, margin_bps, mine, d.position,
            maker_coll, series.get("maker_inventory", 0.0),
        )
        want = d.gap
        room = max_buy if want > 0 else max_sell

        # Blocked — but by WHAT? "no room" was one message for two opposite
        # situations. Here the agent sat at 1.82 contracts on 2.00 USDC of its
        # own collateral, so its taker cap was 1.817 and max_buy was exactly
        # 0.00 while the book had depth to spare. It called that "the book has
        # no room" and stopped, tick after tick, permanently short of a mandate
        # it could have reached by posting a few cents of margin.
        # Not `room <= 0`. The room a margin-bound agent has drifts with the
        # mark, so at 1.82 contracts it was 0.00 one minute and 0.01 the next —
        # and 0.01 is not "unblocked", it is eighteen ticks of gas to close a
        # 0.18 gap. The question is whether OUR OWN margin stops us making the
        # trade we actually want, which is true at 0.01 exactly as it is at 0.
        if room < abs(want):
            from index_api.desk import MARGIN_SAFETY

            per_contract = d.mark * mult * (margin_bps / 10_000)
            t_cap = MARGIN_SAFETY * mine / per_contract if per_contract > 0 else 0.0
            m_cap = MARGIN_SAFETY * maker_coll / per_contract if per_contract > 0 else 0.0
            maker_inv = series.get("maker_inventory", 0.0)
            if not margin_bound(want, t_cap, d.position, m_cap, maker_inv):
                # The book is the constraint, so buying margin changes nothing.
                # Take what room there is if it is worth the gas; otherwise say
                # plainly which side ran out.
                if room <= 0:
                    d.refused = (f"the BOOK has no room this way (max_buy {max_buy}, "
                                 f"max_sell {max_sell}) — more margin of mine would "
                                 "not change that")
                    log_decision(d)
                    break
                d.notes.append(f"book-bound: taking {room} of the {abs(want):.2f} I want")
            add = topup_for(want, d.position, per_contract, MARGIN_SAFETY,
                            mine, TOPUP_MAX_USDC, COLLATERAL_CAP_USDC)
            if add <= 0:
                d.refused = (f"margin-bound at {mine:.2f} USDC and the top-up cap "
                             f"({TOPUP_MAX_USDC:.2f}/tick, {COLLATERAL_CAP_USDC:.2f} "
                             "total) leaves nothing to add — refusing rather than "
                             "quietly holding short of the mandate")
                log_decision(d)
                break
            if DRY_RUN:
                d.notes.append(f"margin-bound — would post {add:.2f} USDC, then trade")
                log_decision(d)
                break
            code, _, raw = circle(["wallet", "execute", "postCollateral(uint256,uint256)",
                                   str(sid), str(int(round(add * 1_000_000))),
                                   "--contract", s.futures_address,
                                   "--address", AGENT_ADDRESS, "--chain", CHAIN])
            if code != 0:
                d.refused = f"margin top-up failed: {raw.strip()[:160]}"
                log_decision(d)
                sys.exit(1)
            time.sleep(3)
            # Re-READ it. A transaction that returned is not collateral posted,
            # and sizing the trade off the number we hoped for is how an agent
            # authorizes an order the contract then reverts.
            mine_after = collateral_or_none(fc, sid, me)
            if mine_after is None or mine_after <= mine:
                d.refused = (f"posted {add:.2f} USDC but the venue does not show it yet "
                             "— not sizing a trade against a hope")
                log_decision(d)
                break
            d.notes.append(f"margin-bound: posted {add:.2f} USDC ({mine:.2f} → {mine_after:.2f})")
            mine = mine_after
            max_buy, max_sell = feasible_qty(
                d.mark, mult, margin_bps, mine, d.position, maker_coll, maker_inv,
            )
            room = max_buy if want > 0 else max_sell
            if room <= 0:
                d.refused = (f"still no room after the top-up (max_buy {max_buy}, "
                             f"max_sell {max_sell})")
                log_decision(d)
                break
        d.qty = round(min(abs(want), room) * (1 if want > 0 else -1), 2)
        d.intent = "buy" if d.qty > 0 else "sell"

        if DRY_RUN:
            d.notes.append("dry run — decided but did not trade")
            log_decision(d)
            break

        # 4) Act, through the agent wallet. (Collateral was provisioned above,
        #    before sizing — `feasible_qty` needs it to give any room at all.)
        code, data, raw = circle(["wallet", "execute", "trade(uint256,int256)",
                                  str(sid), _fmt_qty(d.qty),
                                  "--contract", s.futures_address,
                                  "--address", AGENT_ADDRESS, "--chain", CHAIN])
        if code != 0:
            d.refused = f"trade failed: {raw.strip()[:200]}"
            log_decision(d)
            sys.exit(1)
        body = (data or {}).get("data", data or {})
        d.trade_tx = str(body.get("txHash") or body.get("id") or "")
        d.notes.append("traded through the Circle agent wallet")
        log_decision(d)

        if once:
            break
        time.sleep(INTERVAL_S)

    print(f"\nhedger: done — {spent:.6f} USDC spent on data this run")


if __name__ == "__main__":
    main()
