#!/usr/bin/env python
"""Record which wallets act for which verified human, so the tape can count them.

The manipulation bound ACR publishes is denominated in wallets, and a wallet
costs a funding transfer. Denominating it in humans is the whole point of Module
W, and it needs one fact the chain does not otherwise have: which payer wallets
belong to one person. This writes that fact — as a WINDOW-ROTATED CLUSTER ID,
never a World ID nullifier. See `contracts/src/HumanIdMirror.sol` for why.

    ACR_HUMANID_MIRROR_ADDRESS=0x… uv run python scripts/resolve_humans.py --dry-run
    ACR_HUMANID_MIRROR_ADDRESS=0x… uv run python scripts/resolve_humans.py --commit

RUN THIS BEFORE GENERATING TAPE. `Settlement.human` is stamped at finalize and
the entity is immutable, so a payer that settles before it is resolved is counted
non-human forever and its volume never reaches `humanVolume`. The subgraph flags
it (`Payer.resolvedLate`) but cannot repair it.

Reads the AgentBook — the fixture roster until World Sandbox access exists, and
every fixture human is flagged `sandbox` all the way onto the chain so a rating
that counts them says how much of its human depth is demo. Needs a signer:
``ACR_POSTER_PRIVATE_KEY`` or the Circle poster wallet, the same wallet that
signs oracle prints. Needs ``ACR_HUMANID_SALT``, which must hash to the
commitment the mirror was deployed with — checked here, because a wrong salt
derives cluster ids that match nothing and every surface would then read "no
humans" while every transaction succeeded.

Exit codes: 0 = the tape is up to date (something was resolved, or everything
already was); 1 = it could not be brought up to date — no address, no signer, no
salt, a salt that disagrees with the chain, or an unreachable node. "Nothing to
resolve" is the normal steady state of a recurring chore, so it is a success.
"""

from __future__ import annotations

import argparse
import sys

from acr_core import get_settings
from acr_oracle_client.agentbook import build_agentbook
from acr_oracle_client.humanid import HumanIdMirrorClient, cluster_id, resolve_window, roster_pairs
from acr_oracle_client.signer import CircleAuthError, CircleWalletSigner, circle_key_mismatch
from acr_tape import graph_query

#: The payers the tape actually has. AgentBook answers "who is this wallet?",
#: not "list every human", so the honest enrollment set is the wallets that have
#: really settled — not a roster we hope will go on to trade.
_PAYERS = """
query Payers($first: Int!) {
  payers(orderBy: totalVolume, orderDirection: desc, first: $first) {
    id totalVolume settlementCount
  }
}
"""


def _from_tape(book, s, limit: int) -> list[tuple[str, str, bool]]:
    """(wallet, nullifier, sandbox) for every tape payer AgentBook recognises."""
    if not s.subgraph_url:
        print("  ✗ ACR_SUBGRAPH_URL is unset — cannot read the tape's payers")
        return []
    data = graph_query(s.subgraph_url, _PAYERS, {"first": limit}, s.graph_api_key)
    if not data:
        print("  ✗ the subgraph did not answer — resolving nobody this run")
        return []
    payers = data.get("payers") or []
    out, unresolved = [], 0
    for row in payers:
        wallet = str(row["id"])
        reg = book.lookup(wallet)
        if reg is None:
            unresolved += 1
            continue
        out.append((wallet, reg.nullifier, reg.sandbox))
    print(f"  tape: {len(payers)} payer(s), {len(out)} registered, {unresolved} not")
    return out


def _from_roster(book) -> list[tuple[str, str, bool]]:
    """Every wallet of every human the book can enumerate."""
    pairs = roster_pairs(book)
    if pairs is None:
        print("  this book cannot be enumerated — use --from-tape")
        return []
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="sign and self-check against the chain, but do not broadcast")
    ap.add_argument("--commit", action="store_true",
                    help="actually broadcast (the default is a dry run)")
    ap.add_argument("--from-tape", action="store_true",
                    help="enroll the payers the tape already has, rather than a roster")
    ap.add_argument("--limit", type=int, default=100,
                    help="how many tape payers to consider (--from-tape only)")
    args = ap.parse_args()
    # Dry by default: this writes identity to a public chain, and the one thing
    # that cannot be undone there is having published.
    dry_run = not args.commit
    sys.stdout.reconfigure(line_buffering=True)

    s = get_settings()
    if not s.humanid_mirror_address:
        print("set ACR_HUMANID_MIRROR_ADDRESS to the deployed HumanIdMirror "
              "(make deploy-humanid)")
        return 1
    if not s.humanid_salt:
        print("set ACR_HUMANID_SALT — without it there is no cluster id to record")
        return 1

    client = HumanIdMirrorClient(settings=s)
    if client.signer is None:
        print("  ✗ no signer — set ACR_POSTER_PRIVATE_KEY or the Circle poster wallet")
        return 1

    # A Circle key of the other environment gets a bare 403 for every call, and its
    # own prefix already says so: checked before anything is asked of Circle.
    if isinstance(client.signer, CircleWalletSigner):
        why = circle_key_mismatch(s.circle_api_key or "", s.arc_chain_id)
        if why:
            print(f"  ✗ {why}")
            return 1

    window = client.chain_window()
    if window is None:
        print(f"  ✗ cannot reach {client.rpc_url} — resolving nobody")
        return 1

    try:
        return _resolve(client, s, window, dry_run, args)
    except CircleAuthError as exc:
        # Was sixty lines of SDK traceback on 2026-10-16. The sentence names the cause.
        print(f"  ✗ {exc}")
        return 1


def _resolve(client, s, window: int, dry_run: bool, args) -> int:
    authorized = client.signer_authorized()
    if authorized is False:
        print(f"  ✗ {client.signer.address} is not in the mirror's signer set.")
        print("    Every record() would revert 'bad signer' — and the revert names the")
        print("    signature, not the signer set, so it points at the wrong thing.")
        print("    From the owner:")
        print(f"      cast send {client.mirror_address} 'setSigner(address,bool)' \\")
        print(f"        {client.signer.address} true --rpc-url $ACR_ARC_RPC_URL \\")
        print("        --private-key <owner key>")
        return 1

    matches = client.salt_matches(s.humanid_salt)
    if matches is False:
        print("  ✗ ACR_HUMANID_SALT does not hash to the mirror's SALT_COMMITMENT.")
        print("    Every cluster id derived from it would match nothing on the tape,")
        print("    and every surface would read 'no humans' while the writes succeeded.")
        return 1

    book = build_agentbook(s)
    print(f"  agentbook: {book.source}, window {window}")
    pairs = _from_tape(book, s, args.limit) if args.from_tape else _from_roster(book)

    # Group by human so the output reads as people rather than as rows, and so a
    # fleet's shared cluster is derived once.
    by_nullifier: dict[str, list[str]] = {}
    sandbox_of: dict[str, bool] = {}
    for wallet, nullifier, sandbox in pairs:
        by_nullifier.setdefault(nullifier, []).append(wallet)
        sandbox_of[nullifier] = sandbox

    wrote = skipped = 0
    for nullifier, wallets in by_nullifier.items():
        sandbox = sandbox_of[nullifier]
        cluster = cluster_id(nullifier, s.humanid_salt, window)
        tag = "sandbox" if sandbox else "orb-verified"
        print(f"\n  human ••••{nullifier[-3:]} · {tag} · {len(wallets)} wallet(s)")
        print(f"    cluster 0x{cluster.hex()}")
        group = [(w, nullifier, sandbox) for w in wallets]
        for r in resolve_window(client, group, s.humanid_salt, window, dry_run=dry_run):
            if r.status == "already":
                print(f"    = {r.wallet}  already resolved")
                skipped += 1
            else:
                print(f"    + {r.wallet}  {r.tx or '(not broadcast)'}")
                wrote += 1

    if not by_nullifier:
        print("\n  ✓ nothing to resolve — no wallet the book recognises")
        return 0
    print(f"\n  {wrote} to record, {skipped} already on chain")
    if dry_run:
        print("  dry run — signed and checked against the contract's own digest, "
              "nothing broadcast")
        print("  re-run with --commit to write. Do it BEFORE generating tape:")
        print("  Settlement.human is stamped at finalize and cannot be revised.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
