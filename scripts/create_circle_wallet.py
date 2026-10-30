#!/usr/bin/env python
"""Create a Circle developer-controlled wallet set + EOA wallet on Arc testnet.

Prereq: ``ACR_CIRCLE_API_KEY`` + ``ACR_CIRCLE_ENTITY_SECRET`` in ``.env`` (run
``scripts/register_entity_secret.py`` first). Prints the wallet-set id, wallet
id, and address to paste into ``.env`` as ``ACR_CIRCLE_WALLET_SET_ID`` /
``ACR_CIRCLE_WALLET_ID``.

Account type is **EOA**: the oracle poster signs EIP-712 prints that
``ACROracle`` verifies with ``ecrecover``, which needs an EOA signature (an SCA
would sign via ERC-1271, which ``ecrecover`` can't check).

    uv run python scripts/create_circle_wallet.py
"""

from __future__ import annotations

import sys

from acr_core import get_settings

WALLET_SET_NAME = "ACR Oracle Poster"
BLOCKCHAIN = "ARC-TESTNET"


def _dig(obj, *path):
    """Follow attr path; on miss, raise with the object's dict for debugging."""
    cur = obj
    for p in path:
        nxt = getattr(cur, p, None)
        if nxt is None:
            dump = obj.to_dict() if hasattr(obj, "to_dict") else repr(obj)
            raise AttributeError(f"unexpected response shape at '{p}': {dump}")
        cur = nxt
    return cur


def main() -> None:
    s = get_settings()
    api_key = (s.circle_api_key or "").strip()
    secret = (s.circle_entity_secret or "").strip()
    if not api_key or api_key.startswith("#") or not secret or secret.startswith("#"):
        print(
            "\n  ✗ need ACR_CIRCLE_API_KEY + ACR_CIRCLE_ENTITY_SECRET in .env "
            "(run scripts/register_entity_secret.py first).\n"
        )
        sys.exit(1)
    if s.circle_wallet_id and not s.circle_wallet_id.strip().startswith("#"):
        print("\n  ACR_CIRCLE_WALLET_ID already set — a wallet is already configured. Skipping.\n")
        sys.exit(0)

    from circle.web3 import developer_controlled_wallets as dcw
    from circle.web3 import utils

    client = utils.init_developer_controlled_wallets_client(api_key=api_key, entity_secret=secret)

    # 1) wallet set (each write needs a fresh entity_secret_ciphertext)
    ws_resp = dcw.WalletSetsApi(client).create_wallet_set(
        dcw.CreateWalletSetRequest(
            name=WALLET_SET_NAME,
            entity_secret_ciphertext=utils.generate_entity_secret_ciphertext(api_key, secret),
        )
    )
    wallet_set_id = _dig(ws_resp, "data", "wallet_set", "id")

    # 2) EOA wallet on Arc testnet in that set
    w_resp = dcw.WalletsApi(client).create_wallet(
        dcw.CreateWalletRequest(
            wallet_set_id=wallet_set_id,
            blockchains=[BLOCKCHAIN],
            count=1,
            account_type="EOA",
            entity_secret_ciphertext=utils.generate_entity_secret_ciphertext(api_key, secret),
        )
    )
    wallets = _dig(w_resp, "data", "wallets")
    wallet = wallets[0]
    wallet_id = getattr(wallet, "id", None)
    address = getattr(wallet, "address", None)

    print("\n  ✓ created wallet set + EOA wallet on Arc testnet.\n")
    print("  Add these to .env:")
    print(f"    ACR_CIRCLE_WALLET_SET_ID={wallet_set_id}")
    print(f"    ACR_CIRCLE_WALLET_ID={wallet_id}\n")
    print(f"  wallet address: {address}")
    print("    → fund it with testnet USDC (faucet.circle.com, Arc Testnet) — it pays Arc gas, and")
    print("    → authorize it as an ACROracle signer, e.g.:")
    print(f"       cast send $ACR_ORACLE_ADDRESS 'setSigner(address,bool)' {address} true \\")
    print("            --rpc-url $ACR_ARC_RPC_URL --private-key $DEPLOYER_PRIVATE_KEY\n")


if __name__ == "__main__":
    main()
