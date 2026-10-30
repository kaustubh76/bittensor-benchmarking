#!/usr/bin/env python3
"""Prove a human to the gate and read their TCA — the World path, runnable by a judge.

    uv run python scripts/prove_human.py                       # demo buyer-1 against production
    uv run python scripts/prove_human.py --label acr-buyer-4   # the solo human
    uv run python scripts/prove_human.py --api http://127.0.0.1:8000 --key 0x...

What happens, in order, and what each step proves:

  1. GET /tca/human with no proof  → a 401 CHALLENGE: a single-use nonce, the
     resource it is bound to, the header to answer in. (The gate asks; it never
     takes a credential it did not ask for.)
  2. Sign a CAIP-122 message naming that nonce and that resource with the wallet's
     key — the same message shape World's AgentKit carries — and base64 it.
  3. GET /tca/human again with the proof → the gate recovers the signer, looks the
     wallet up in AgentBook, derives the human's CLUSTER for this rotation window,
     and answers with one TCA across EVERY wallet that human owns. The response
     names the cluster and the wallet count and never the wallet list.

The demo buyers' keys derive from public labels (`demo_humans.py`), so a judge can
run this with nothing secret in hand. A key of your own works the moment the wallet
is registered in the AgentBook the gate reads (`/humanid/info` says which).
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

from acr_oracle_client.demo_humans import DemoBuyer

# Arc MAINNET, and OVERRIDABLE — this was hardcoded to the Arc testnet host,
# suspended since 2026-09-15, so the script could not be pointed elsewhere.
API = os.environ.get("ACR_API_URL", "https://acr-api-mainnet.onrender.com").rstrip("/")
RESOURCE = "/tca/human"


def _get(url: str, headers: dict | None = None, timeout: float = 40.0) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def proof_header(key: str, *, nonce: str, resource: str, host: str) -> tuple[str, str]:
    """A signed CAIP-122 payload, base64-JSON — what the gate's AgentKit verifier reads."""
    from eth_account import Account
    from eth_account.messages import encode_defunct

    acct = Account.from_key(key)
    issued = dt.datetime.now(dt.UTC).isoformat()
    raw = (
        f"{host} wants you to sign in with your account:\n{acct.address}\n\n"
        f"URI: {resource}\nVersion: 1\nChain ID: 480\nNonce: {nonce}\nIssued At: {issued}"
    )
    sig = Account.sign_message(encode_defunct(text=raw), private_key=key).signature.hex()
    payload = {
        "address": acct.address, "nonce": nonce, "issuedAt": issued, "uri": resource,
        "chainId": "eip155:480", "signedMessage": raw,
        "signature": sig if sig.startswith("0x") else "0x" + sig, "type": "eip191",
    }
    return base64.b64encode(json.dumps(payload).encode()).decode(), acct.address


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=API)
    ap.add_argument("--label", default="acr-buyer-1", help="a demo buyer label (key derives from it, publicly)")
    ap.add_argument("--key", default=None, help="your own 0x key instead (the wallet must be in AgentBook)")
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args()

    key = a.key or DemoBuyer(a.label).private_key
    url = f"{a.api}{RESOURCE}?days={a.days}"
    host = a.api.split("//", 1)[-1]

    print(f"gate      {a.api}")
    st, info = _get(f"{a.api}/humanid/info")
    print(f"verifier  {info.get('backend')} · roster {info.get('agentbook')} · sandbox {info.get('sandbox')}"
          f" · salt matches commitment: {info.get('salt_matches_commitment')}")

    print("\n1 · ask without a proof")
    st, ch = _get(url)
    if st != 401 or not ch.get("nonce"):
        print(f"  ✗ expected a 401 challenge, got {st}: {json.dumps(ch)[:200]}")
        return 1
    print(f"  ✓ 401 · scheme {ch.get('scheme')} · answer in header {ch.get('header')} · nonce {ch['nonce'][:10]}… "
          f"· bound to {ch.get('resource')} · spendable {ch.get('expires_in_seconds')}s")

    print("\n2 · sign the challenge")
    header, address = proof_header(key, nonce=ch["nonce"], resource=RESOURCE, host=host)
    print(f"  ✓ CAIP-122 message signed by {address} ({len(header)} bytes, base64 JSON)")

    print("\n3 · present it")
    st, body = _get(url, {str(ch.get("header") or "HUMAN-PROOF"): header})
    if st != 200:
        print(f"  ✗ {st}: {body.get('detail') or json.dumps(body)[:300]}")
        return 1
    human = body.get("human") or {}
    print(f"  ✓ 200 · the gate resolved this wallet to ONE person: cluster {str(human.get('cluster'))[:12]}… "
          f"· window {human.get('window')} · {human.get('wallet_count')} wallet(s) — the list is never returned")
    if body.get("available"):
        print(f"  ✓ TCA across all of them: {body.get('purchases')} purchases · spent ${body.get('spent_usdc')} "
              f"· vw slippage {body.get('vw_slippage_bp')} bp · overpaid ${body.get('overpaid_usdc')}")
        rr = body.get("reroute")
        if rr:
            print(f"    reroute {rr['from'][:8]}… → {rr['to'][:8]}… saves {rr['saving_bp']} bp ({rr.get('note')})")
    else:
        print(f"  · TCA not available: {body.get('reason')}")
        return 1

    print("\n4 · the nonce is spent")
    st, again = _get(url, {"HUMAN-PROOF": header})
    print(f"  {'✓' if st == 401 else '✗'} replaying the same proof: {st} ({again.get('detail', '')})")
    print("\nprove-human: A PERSON, NOT A WALLET, WAS THE UNIT" if st == 401 else "")
    return 0 if st == 401 else 1


if __name__ == "__main__":
    sys.exit(main())
