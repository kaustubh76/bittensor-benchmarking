#!/usr/bin/env python
"""Public Desk preflight — every read-only check the desk E2E depends on.

Before a visitor (or the E2E driver) opens a Circle user-controlled wallet and
trades, verify — with friendly ✓/✗ output and **no writes** (verify_deploy.py
idiom):

  * the RPC's chain id matches ``ACR_ARC_CHAIN_ID``,
  * ``ACR_FUTURES_ADDRESS`` is configured and has bytecode,
  * the desk's series is open, unexpired (≥ 2h left), with roster headroom,
  * the oracle mark is live and fresh,
  * the margin math: how many contracts the 0.5 USDC faucet stake supports,
    and the maker's headroom in both directions (a desk BUY makes the maker
    shorter; a SELL makes it longer),
  * the custody wallet (faucet source) resolves and holds enough USDC for at
    least a few drips,
  * how many faucet-ledger slots are already spent.

Exit codes: 0 = clear to run; 1 = a gate failed.

    uv run python scripts/desk_preflight.py            # defaults to ACR-INF
    SEED_INDEX=ACR-GPU uv run python scripts/desk_preflight.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from acr_core import get_settings
from acr_oracle_client import FuturesClient, OracleClient, select_series_for_index
from acr_oracle_client.futures import _rpc_retry
from index_api.desk import (
    FAUCET_GLOBAL_CAP,
    FAUCET_USDC,
    MARGIN_SAFETY,
    MAX_QTY,
    MIN_QTY,
)
from index_api.keeper import live_indices

#: An optional FILTER, not a selection. Which books exist is a fact about the
#: chain (see index_api.keeper.live_indices), and certifying one while the desk
#: offers three is how the index a reader actually lands on goes unchecked.
ONLY = os.environ.get("SEED_INDEX", "").strip()
MIN_EXPIRY_S = 2 * 3600
# MARGIN_SAFETY and MAX_QTY are IMPORTED from the desk this file gates. They
# used to be restated here at 0.95 against the desk's 0.90 — a preflight more
# optimistic than the thing it certifies — and taker_max was left unclamped, so
# on a cheap index (ACR-GPU, mark 0.011) a 0.50 stake "supported" 21 contracts
# against a desk that will never quote more than 2.0, and the maker-absorption
# check then failed a perfectly healthy book. It reported BLOCKED on the very
# index the UI defaults to.

_MARGIN_ABI = [{"type": "function", "name": "MARGIN_BPS", "stateMutability": "view",
                "inputs": [], "outputs": [{"name": "", "type": "uint256"}]}]


def _check(ok: bool, label: str) -> bool:
    print(f"  {'✓' if ok else '✗'} {label}")
    return ok


def main() -> None:
    s = get_settings()
    ok = True

    print("Public Desk preflight (read-only)")
    if not s.futures_address:
        _check(False, "ACR_FUTURES_ADDRESS unset — the desk 503s without a venue")
        sys.exit(1)

    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(s.arc_rpc_url, request_kwargs={"timeout": 15}))
    chain_id = _rpc_retry(lambda: w3.eth.chain_id)
    ok &= _check(chain_id == s.arc_chain_id, f"chain id {chain_id} == {s.arc_chain_id}")
    code = _rpc_retry(w3.eth.get_code, Web3.to_checksum_address(s.futures_address))
    ok &= _check(len(code) > 2, f"bytecode at futures venue {s.futures_address}")

    fc = FuturesClient(rpc_url=s.arc_rpc_url, futures_address=s.futures_address)
    all_series = fc.read_all_series()
    oracle = OracleClient(rpc_url=s.arc_rpc_url, oracle_address=s.oracle_address or None)
    margin_c = w3.eth.contract(address=Web3.to_checksum_address(s.futures_address), abi=_MARGIN_ABI)
    try:
        margin_bps = int(_rpc_retry(margin_c.functions.MARGIN_BPS().call))
    except Exception:
        margin_bps = 2000

    now_chain = int(_rpc_retry(lambda: w3.eth.get_block("latest"))["timestamp"])
    books = live_indices(all_series, now_chain, [ONLY] if ONLY else None)
    if not books:
        _check(False, f"no open series{f' for {ONLY}' if ONLY else ''} — run scripts/futures_roll.py")
        sys.exit(1)
    print(f"  · {len(books)} live book(s): {', '.join(books)}")

    for INDEX in books:
        ok &= _book(fc, oracle, INDEX, all_series, margin_bps)

    print()
    ok = _global(s, w3, ok)
    print("preflight:", "CLEAR TO RUN" if ok else "BLOCKED")
    sys.exit(0 if ok else 1)


def _book(fc, oracle, INDEX, all_series, margin_bps) -> bool:
    """Every gate, for ONE book. Called for each live index."""
    ok = True
    print(f"\n  ── {INDEX} ──")
    series = select_series_for_index(all_series, INDEX)
    if series is None:
        return _check(False, f"no open series for {INDEX}")
    sid, mult, maker = series["series_id"], series["multiplier"], series["maker"]
    left = series["expiry_ts"] - time.time()
    ok &= _check(left >= MIN_EXPIRY_S,
                 f"series {sid} ({INDEX}, mult {mult}) expires in {left / 3600:.1f}h (≥ 2h)")

    desk = fc.read_desk(INDEX, all_series=all_series) or {}
    traders = desk.get("trader_count", 0)
    ok &= _check(traders < 120, f"trader roster {traders}/128 (headroom below 120)")

    print_ = oracle.read_latest(INDEX)
    if print_ is None:
        return _check(False, f"no oracle print for {INDEX}")
    mark = print_["value"]
    age = time.time() - print_["posted_at"]
    ok &= _check(mark > 0 and age < 3600, f"oracle mark {mark:.4f} posted {age / 60:.0f}m ago")

    per_contract = mark * mult * margin_bps / 10_000
    # CLAMPED at MAX_QTY: that is the largest trade the desk will ever quote, so
    # a stake covering more of them is capacity nobody can use — and asserting
    # the maker absorbs the unclamped figure fails a healthy cheap book.
    taker_max = min(MARGIN_SAFETY * FAUCET_USDC / per_contract, MAX_QTY)
    print(f"  · margin {margin_bps}bps → {per_contract:.4f} USDC/contract; "
          f"{FAUCET_USDC} stake supports ±{taker_max:.2f} contracts (desk cap {MAX_QTY})")
    ok &= _check(taker_max >= MIN_QTY, f"faucet stake supports a tradable qty (≥ {MIN_QTY})")

    maker_coll = fc.collateral_of(sid, maker) or 0.0
    maker_inv = desk.get("maker_inventory", 0.0)
    maker_cap = MARGIN_SAFETY * maker_coll / per_contract
    # A desk BUY of q mirrors the maker to inv−q; a SELL to inv+q.
    room_buy = maker_cap + maker_inv
    room_sell = maker_cap - maker_inv
    print(f"  · maker inv {maker_inv:+.2f}, collateral {maker_coll:.2f} USDC → "
          f"absorbs BUY ≤ {room_buy:.2f} / SELL ≤ {room_sell:.2f} contracts")
    ok &= _check(min(room_buy, room_sell) >= taker_max,
                 "maker margin absorbs a max-size desk trade in both directions")
    return ok


def _global(s, w3, ok: bool) -> bool:
    """Gates that belong to the DESK, not to any one book — the faucet's source
    of stakes and its ledger. Run once, after every book has been checked."""
    from web3 import Web3

    # Custody wallet — the faucet's source of stakes (live Circle lookup).
    try:
        from acr_oracle_client.signer import CircleWalletSigner

        signer = CircleWalletSigner(
            wallet_id=s.circle_wallet_id, api_key=s.circle_api_key,
            entity_secret=s.circle_entity_secret, base_url=s.circle_base_url,
        )
        custody = signer.address
        bal = _rpc_retry(w3.eth.get_balance, Web3.to_checksum_address(custody)) / 1e18
        ok &= _check(bal >= 3 * FAUCET_USDC,
                     f"custody wallet {custody[:10]}… holds {bal:.2f} USDC (≥ 3 drips)")
    except Exception as exc:
        ok &= _check(False, f"custody wallet lookup failed: {exc}")

    ledger_path = Path(s.webhook_log_path or "data/x.jsonl").parent / "desk_faucet.jsonl"
    spent = 0
    if ledger_path.exists():
        rows = [json.loads(r) for r in ledger_path.read_text().splitlines() if r.strip()]
        spent = len({r["address"] for r in rows})
    ok &= _check(spent < FAUCET_GLOBAL_CAP, f"faucet ledger {spent}/{FAUCET_GLOBAL_CAP} slots spent")
    return ok


if __name__ == "__main__":
    main()
