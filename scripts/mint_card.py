#!/usr/bin/env python3
"""Mint a scoped agent card, and print it — the one credential this repo emits.

    ACR_READER_PRIVATE_KEY=0x… uv run python scripts/mint_card.py --business acr-fleet
    ACR_READER_PRIVATE_KEY=0x… uv run python scripts/mint_card.py --business acr-fleet --curl

Why this script exists, and why it is the only one that prints a card.

Three places already mint cards — `scripts/verify_live.py`, `scripts/demo_agent.py`
and the three TypeScript encoders — and every one of them deliberately keeps the
header to itself: they mint in order to PRESENT a card on the same request, and a
header on a terminal's scrollback is a bearer credential somebody can read over
your shoulder. None of them is the right thing to extend. `verify_live.py`'s
minter is a cached process-global tied to one env var whose purpose is to present,
`demo_agent.py` hardcodes `role="reader"` and promises in its own header that it
touches no real key, and the TS encoders hardcode `scopeHash: ZERO32` and would
need the field threaded through three objects first.

So: a separate script, shaped after `scripts/prove_human.py` — the only other
credential-minting CLI here — and honest about what it hands you.

WHAT YOU GET is a bearer token, not a key. It is scoped to one business, it dies
in fifteen minutes (`MAX_TTL_S`), and it cannot spend: the card's `role` is
`reader` and no spending path consults a card at all. That is precisely why it is
safe to paste into a field on /spend and a private key never is.

AND IT CANNOT BE REVOKED. `docs/AGENT-MODULE.md` records that as a design
decision rather than an oversight — "the 15-minute bound IS the revocation
window. A registry would fix that and would also make the scheme permissioned,
which is the property being bought here." So a card you paste somewhere you
regret is live until it expires. Mint a short one.

The key comes from the ENVIRONMENT, never from argv: an argv shows in `ps` to
every other process on the box, which is the lesson `apps/agent/src/deposit.ts`
already carries for the buyer's key.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from acr_oracle_client.agentcard import MAX_TTL_S, encode_header, mint, scope_hash, sign_card
from acr_oracle_client.signer import LocalKeySigner

KEY_ENV = "ACR_READER_PRIVATE_KEY"


def business_read_scope(slug: str) -> str:
    """The scope string the press expects, duplicated for one deliberate reason.

    Importing `index_api.agentgate` would pull the whole FastAPI app in to read
    one f-string, and this script has to run for somebody who has only the
    client package. `test_operator_scope.py` pins the press's side of it, and the
    `--verify` output below prints the hash so the two can be compared without
    trusting either.
    """
    return f"read:business:{slug}"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--business", required=True, help="the registry slug to scope the card to")
    ap.add_argument(
        "--chain-id",
        type=int,
        default=int(os.environ.get("ACR_ARC_CHAIN_ID", "5042")),
        help="the chain the card's EIP-712 domain names (default: ACR_ARC_CHAIN_ID, else 5042)",
    )
    ap.add_argument("--audience", default="acr-index-api", help="read it from /agent/challenge")
    ap.add_argument(
        "--ttl",
        type=int,
        default=300,
        help=f"seconds, at most {MAX_TTL_S}. Shorter is better: there is no revocation",
    )
    ap.add_argument("--curl", action="store_true", help="print a ready-to-run curl instead")
    ap.add_argument("--api", default=os.environ.get("ACR_API_URL", ""), help="for --curl")
    args = ap.parse_args(argv)

    raw = (os.environ.get(KEY_ENV) or "").strip()
    if not raw:
        print(
            f"  ✗ set {KEY_ENV} to the key you want to sign with.\n"
            "    It must be the business's treasury, or an address in its `readers`\n"
            "    list — which is a commit to businesses.json, not an API call.\n"
            "    Never pass a key as an argument: argv is visible in `ps`.",
            file=sys.stderr,
        )
        return 2

    scope = business_read_scope(args.business)
    signer = LocalKeySigner(raw)
    card = mint(
        signer.address,
        name="acr-reader",
        role="reader",
        audience=args.audience,
        scopes=[scope],
        ttl_s=args.ttl,
    )
    header = encode_header(card, sign_card(card, signer, args.chain_id))

    if args.curl:
        base = args.api.rstrip("/") or "https://acr-api-mainnet.onrender.com"
        print(
            f"curl -sS -H 'AGENT-CARD: {header}' "
            f"'{base}/operator/statement/{args.business}'"
        )
        return 0

    left = card.expires_at - int(time.time())
    print(f"  signer    {signer.address}")
    print(f"  business  {args.business}")
    print(f"  scope     {scope}")
    # The hash is the whole credential, so print it: it is what the gate compares,
    # and it lets a reader check this against the press's own computation rather
    # than trusting that both sides spell the scope the same way.
    print(f"  hash      {scope_hash([scope])}")
    print(f"  chain     {args.chain_id}")
    print(f"  expires   in {left}s — there is no revocation, only this")
    print()
    print(header)
    print()
    print("  Paste that into the card field on /spend, or send it as the AGENT-CARD header.")
    print("  It is a bearer token: it cannot spend, and anyone holding it reads what you read.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
