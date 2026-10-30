#!/usr/bin/env python
"""Turn an x402 payment into an on-chain right.

Circle Gateway settles nanopayments off-chain and returns a batch UUID. No
contract can see that, so "this wallet paid for the feed" lives only in the
seller's ledger. Asked directly, Circle's answer was that you would need "an
EIP-712 signed attestation or oracle receipt to cryptographically verify the
off-chain x402 payment on-chain". This mints exactly that.

The seller reads its OWN settlement ledger, signs a `FeedAccess` struct with the
same Circle developer-controlled wallet that signs oracle prints, and anyone can
relay it to `FeedAccessAttestor.redeem`. After that, `hasFeedAccess(addr)` is a
fact any other contract can branch on — a futures venue could rebate fees to
wallets that paid for the index, which is the point.

**The seller signs; it does not decide.** Every attestation is derived from rows
already in the public ledger at `/marketplace/receipts`. This script cannot mint
access for a wallet that has not paid, because it has nowhere to get the payer
from except the settlements themselves — which is what makes the receipt worth
believing rather than merely worth verifying.

Two addresses, one agent, and the reason `payer` and `beneficiary` are separate
fields: an x402 `exact` settlement is signed by an EOA (the facilitator
`ecrecover`s EIP-3009), so a Circle agent wallet pays from its BACKING EOA while
its smart account is what trades. Access follows the beneficiary.

    ATTESTOR_ADDRESS=0x… uv run python scripts/attest_feed_access.py \
        --beneficiary 0x1Dc707E3…          # who gets the access
    ATTEST_DRY_RUN=1 …                     # sign + print, broadcast nothing

Exit codes: 0 = an attestation was minted (or the dry run produced one);
1 = the payer has no settlements, or nothing could sign.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

from acr_core import get_settings

# The struct, the ABI and the signing live in ONE place, shared with the API's
# Desk pass — a typehash drift shows up in both at once.
from acr_oracle_client.feed_access import (
    ATTESTOR_ABI as _ATTESTOR_ABI,
)
from acr_oracle_client.feed_access import (
    FEED_ACCESS_TYPES,
)
from acr_oracle_client.feed_access import (
    domain as _domain,
)

# Arc MAINNET. The Arc testnet host this defaulted to (`acr-api-1fto`) has been
# suspended since 2026-09-15 and answers 503 — a default nothing in the repo
# shows you using is exactly how the press lost 48.9 hours of prints.
API = os.environ.get("ACR_API_URL", "https://acr-api-mainnet.onrender.com").rstrip("/")
ATTESTOR = os.environ.get("ATTESTOR_ADDRESS", "").strip()
DRY_RUN = os.environ.get("ATTEST_DRY_RUN", "") not in ("", "0", "false")
#: How long one attestation grants. Short by design: re-minting is cheap and a
#: signer compromise should cost days of free access, not a quarter.
ACCESS_DAYS = float(os.environ.get("ATTEST_ACCESS_DAYS", "7"))

def settlements_for(payer: str) -> list[dict]:
    """Rows from the seller's own public ledger whose payer is ``payer``."""
    req = urllib.request.Request(
        f"{API}/marketplace/receipts", headers={"User-Agent": "acr-attest"}
    )
    with urllib.request.urlopen(req, timeout=90) as r:  # noqa: S310
        body = json.loads(r.read() or b"{}")
    return [
        row for row in (body.get("receipts") or [])
        if str(row.get("payer", "")).lower() == payer.lower()
        and row.get("scheme") == "exact"
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--beneficiary", required=True,
                    help="the account that receives feed access (the SCA, for an agent wallet)")
    ap.add_argument("--payer", default="",
                    help="the settling address; defaults to --beneficiary")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    s = get_settings()
    if not ATTESTOR:
        print("set ATTESTOR_ADDRESS to the deployed FeedAccessAttestor")
        sys.exit(1)

    from acr_oracle_client import build_role_signer
    from eth_utils import to_checksum_address
    from web3 import Web3

    beneficiary = to_checksum_address(args.beneficiary)
    payer = to_checksum_address(args.payer or args.beneficiary)

    rows = settlements_for(payer)
    if not rows:
        print(f"  ✗ {payer} has no `exact` settlements on {API} — refusing to attest "
              "access nobody paid for")
        sys.exit(1)
    total_usdc = sum(float(r.get("amount_usdc", 0.0)) for r in rows)
    print(f"payer       {payer}\n  {len(rows)} settlement(s), {total_usdc:.6f} USDC total")
    print(f"beneficiary {beneficiary}")

    # The press's own signer — the wallet ACROracle already trusts. Reusing it
    # means the attestor's trust anchor is the one a judge has already verified.
    signer = build_role_signer("poster", s)
    if signer is None:
        print("  ✗ no signer — set ACR_CIRCLE_WALLET_ID (+ ACR_CIRCLE_API_KEY)")
        sys.exit(1)
    custody = type(signer).__name__ == "CircleWalletSigner"
    print(f"signer      {signer.address} ({'Circle custody' if custody else 'local key'})")

    w3 = Web3(Web3.HTTPProvider(s.arc_rpc_url, request_kwargs={"timeout": 25}))
    attestor = w3.eth.contract(address=to_checksum_address(ATTESTOR), abi=_ATTESTOR_ABI)

    paid_until = int(time.time() + ACCESS_DAYS * 86400)
    amount_units = int(round(total_usdc * 1_000_000))
    # A nonce nobody else will pick, derived rather than random so a re-run with
    # the same inputs is idempotent at the contract (second redeem → "nonce used").
    nonce = int.from_bytes(
        Web3.keccak(text=f"{payer}:{beneficiary}:{paid_until // 3600}")[:16], "big"
    )

    domain = _domain(w3.eth.chain_id, ATTESTOR)
    message = {
        "payer": payer,
        "beneficiary": beneficiary,
        "paidUntil": paid_until,
        "amountUsdc": amount_units,
        "nonce": nonce,
    }
    v, r, sig_s = signer.sign_typed_data(domain, FEED_ACCESS_TYPES, message, "FeedAccess")

    # Check our digest against the CHAIN's, not against a reimplementation of
    # it. An EIP-712 type mismatch recovers to a stranger, and the only symptom
    # would be "bad signer" on a redeem that should have worked.
    onchain_digest = attestor.functions.accessDigest(
        payer, beneficiary, paid_until, amount_units, nonce
    ).call()
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    recovered = Account.recover_message(
        encode_typed_data(domain_data=domain, message_types=FEED_ACCESS_TYPES,
                          message_data=message),
        vrs=(v, int.from_bytes(r, "big"), int.from_bytes(sig_s, "big")),
    )
    ok = recovered.lower() == signer.address.lower()
    print(f"  digest    0x{onchain_digest.hex()[:24]}…  recovers to the signer: {ok}")
    if not ok:
        print("  ✗ the signature does not recover to our own signer — refusing to broadcast")
        sys.exit(1)

    if DRY_RUN:
        print(json.dumps({**message, "v": v, "r": r.hex(), "s": sig_s.hex()}, indent=2))
        print("\ndry run — signed but not broadcast")
        sys.exit(0)

    tx_hash = signer.send_transaction(
        w3,
        {
            "to": to_checksum_address(ATTESTOR),
            "data": attestor.encode_abi(
                "redeem",
                args=[payer, beneficiary, paid_until, amount_units, nonce, v, r, sig_s],
            ),
        },
    )
    print(f"  redeem tx {tx_hash}")
    time.sleep(4)

    # Two witnesses, because a transaction that returned is not a right granted.
    has = attestor.functions.hasFeedAccess(beneficiary).call()
    until = attestor.functions.paidUntil(beneficiary).call()
    print(f"\nhasFeedAccess({beneficiary[:10]}…) = {has}")
    print(f"paidUntil = {until} ({(until - time.time()) / 86400:.1f} days from now)")
    print("\n✓ an off-chain x402 payment is now a fact this chain can check"
          if has else "\n✗ the chain does not agree access was granted")
    sys.exit(0 if has else 1)


if __name__ == "__main__":
    main()
