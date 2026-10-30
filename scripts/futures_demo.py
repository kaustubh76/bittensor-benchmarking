#!/usr/bin/env python
"""On-chain futures round trip — the instrument desk, live.

Deploys ACROracle + MockUSDC + ACRFutures to a local anvil node, posts a signed
print, opens a weekly series, has a maker + taker post USDC collateral and trade,
reads the live desk (maker inventory + mark-to-oracle PnL), then warps past
expiry and cash-settles against the oracle — proving the whole pillar-4 loop
on-chain.

    anvil &                        # (or `make anvil`)
    uv run python scripts/futures_demo.py

Point it at Arc testnet instead of anvil for the real demo (operator, funded
keys): set RPC + ACR_FUTURES_ADDRESS/ACR_ORACLE_ADDRESS and the maker/taker keys.
Skips gracefully if anvil is unreachable or the contracts aren't built.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from acr_oracle_client import FuturesClient, OracleClient
from index_api.store import PrintStore

ROOT = Path(__file__).resolve().parent.parent
RPC = "http://127.0.0.1:8545"
# anvil default accounts (well-known dev keys; local-only, never real value).
OWNER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
MAKER_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
TAKER_KEY = "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"
INDEX = "ACR-INF"
MULT = 1000
ERC20_ABI = [
    {"type": "function", "name": "mint", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}], "outputs": []},
    {"type": "function", "name": "approve", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]


def _artifact(name: str) -> tuple[list, str]:
    art = json.loads((ROOT / f"contracts/out/{name}.sol/{name}.json").read_text())
    return art["abi"], art["bytecode"]["object"]


def _send(w3, acct, fn) -> None:
    tx = fn.build_transaction(
        {"from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
         "gas": 3_000_000, "chainId": w3.eth.chain_id}
    )
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(raw), timeout=30)


def _deploy(w3, acct, name: str, *args) -> str:
    abi, bytecode = _artifact(name)
    c = w3.eth.contract(abi=abi, bytecode=bytecode)
    tx = c.constructor(*args).build_transaction(
        {"from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
         "gas": 5_000_000, "chainId": w3.eth.chain_id}
    )
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    rcpt = w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(raw), timeout=30)
    return rcpt.contractAddress


def main() -> None:
    try:
        from eth_account import Account
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 3}))
        if not w3.is_connected():
            raise ConnectionError
    except Exception:
        print("\n  anvil not reachable at 127.0.0.1:8545 — start it with `make anvil`.\n")
        sys.exit(0)
    if not (ROOT / "contracts/out/ACRFutures.sol/ACRFutures.json").exists():
        print("\n  contracts not built — run `forge build` in contracts/ first.\n")
        sys.exit(0)

    owner = Account.from_key(OWNER_KEY)
    maker = Account.from_key(MAKER_KEY)
    taker = Account.from_key(TAKER_KEY)

    print(f"\n  deploying the futures desk to anvil (owner {owner.address[:10]}…)")
    oracle_addr = _deploy(w3, owner, "ACROracle")
    usdc_addr = _deploy(w3, owner, "MockUSDC")
    futures_addr = _deploy(w3, owner, "ACRFutures", oracle_addr, usdc_addr, 2000)
    print(f"  ACROracle  {oracle_addr}")
    print(f"  MockUSDC   {usdc_addr}")
    print(f"  ACRFutures {futures_addr}\n")

    # A signed print so the series has a mark to open against.
    store = PrintStore()
    store.refresh(ts=3600.0)
    OracleClient(rpc_url=RPC, oracle_address=oracle_addr, private_key=OWNER_KEY).post(store.latest[INDEX])
    mark = store.latest[INDEX].value
    print(f"  ① posted {INDEX} @ {mark:.5f} on-chain (the settlement mark)")

    # Maker + taker fund collateral (mint + approve the mock USDC).
    usdc = w3.eth.contract(address=w3.to_checksum_address(usdc_addr), abi=ERC20_ABI)
    for acct in (maker, taker):
        _send(w3, acct, usdc.functions.mint(acct.address, 10_000 * 10**6))
        _send(w3, acct, usdc.functions.approve(futures_addr, 2**256 - 1))

    owner_fc = FuturesClient(rpc_url=RPC, futures_address=futures_addr, private_key=OWNER_KEY)
    maker_fc = FuturesClient(rpc_url=RPC, futures_address=futures_addr, private_key=MAKER_KEY)
    taker_fc = FuturesClient(rpc_url=RPC, futures_address=futures_addr, private_key=TAKER_KEY)

    expiry = int(time.time()) + 60  # short so we can warp past it
    owner_fc.open_series(INDEX, expiry, MULT, maker.address)
    maker_fc.post_collateral(0, 2_000)
    taker_fc.post_collateral(0, 2_000)
    print(f"  ② opened series 0 ({INDEX}, maker {maker.address[:10]}…), both posted $2,000")

    taker_fc.trade(0, 3.0)  # taker longs 3 contracts; maker takes the mirror
    print("  ③ taker LONG 3 contracts @ mark — maker is short 3 (net zero)\n")

    desk = owner_fc.read_desk(INDEX)
    print(f"  live desk: maker inventory {desk['maker_inventory']:+.1f} contracts · "
          f"unrealized ${desk['maker_unrealized_usdc']:+,.2f} · "
          f"open interest {desk['open_interest']:.1f} · traders {desk['trader_count']}")

    # Warp past expiry, post a fresh settlement print, and cash-settle.
    w3.provider.make_request("evm_increaseTime", [120])
    w3.provider.make_request("evm_mine", [])
    store.refresh(ts=expiry + 30)  # a fresh print, ts past expiry
    OracleClient(rpc_url=RPC, oracle_address=oracle_addr, private_key=OWNER_KEY).post(store.latest[INDEX])
    settle_mark = store.latest[INDEX].value
    owner_fc.settle(0)
    settled = owner_fc.read_all_series()[0]
    print(f"\n  ④ settled series 0 against the oracle @ {settle_mark:.5f} "
          f"(settled={settled['settled']}, price={settled['settlement_price']:.5f})")
    print("  → futures cash-settled on-chain against ACROracle. Pillar 4 is live.\n")


if __name__ == "__main__":
    main()
