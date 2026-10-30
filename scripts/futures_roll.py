#!/usr/bin/env python
"""Roll the futures venue onto a fresh series — safely, and idempotently.

A series expires. When it does, ``trade`` reverts and the desk has nothing to
offer, so the venue needs a successor opened before the old one dies. This does
that, and refuses to pretend it worked when it didn't.

Why not ``scripts/futures_seed.py``: with ``SEED_FORCE_NEW=1`` it opens the
series, then throws on an ``approve`` for the configured taker key, prints a
friendly note and **exits 0** — leaving an opened, uncollateralized series
reported as success. A maker with no collateral cannot absorb a single desk
trade, so the desk looks live and reverts on first contact. This script asserts
the collateral landed and exits non-zero otherwise.

Safe to run from cron: it no-ops when a healthy series already has life left.

Signs as the **maker role**. With ``ACR_CIRCLE_MAKER_WALLET_ID`` configured that
is a Circle developer-controlled wallet and no private key is involved at all;
``MAKER_PRIVATE_KEY`` still overrides for anvil and offline runs. Cron is
exactly why this role is developer-controlled rather than a Circle *agent*
wallet — an agent wallet's session is an email OTP that expires, which cannot
roll a series at 02:17 UTC. See docs/WALLETS.md.

    uv run python scripts/futures_roll.py                          # == make futures-roll
    MAKER_PRIVATE_KEY=0x… uv run python scripts/futures_roll.py    # anvil / offline

    ROLL_INDEX=ACR-INF ROLL_MULT=10 ROLL_COLLATERAL=1.5 ROLL_EXPIRY_DAYS=14
    ROLL_MIN_LIFE_H=24     # below this much life left, roll
    ROLL_GAS_FLOOR=0.75    # never spend the maker below this many USDC of gas
"""

from __future__ import annotations

import os
import sys
import time

from acr_core import get_settings
from acr_oracle_client import FuturesClient, build_role_signer
from acr_oracle_client.futures import _rpc_retry, collateral_or_none

INDEX = os.environ.get("ROLL_INDEX", "ACR-INF")
MULT = int(os.environ.get("ROLL_MULT", "10"))
COLLATERAL = float(os.environ.get("ROLL_COLLATERAL", "1.5"))
#: 14 days, matching the keeper (index_api/keeper.py). They were 7 and 14:
#: two paths that both roll this venue, opening series of different lengths
#: depending on which one happened to fire.
EXPIRY_DAYS = float(os.environ.get("ROLL_EXPIRY_DAYS", "14"))
MIN_LIFE_H = float(os.environ.get("ROLL_MIN_LIFE_H", "24"))
#: On Arc, USDC *is* the gas token — posted collateral comes straight out of the
#: maker's ability to pay for its next transaction. Never spend below this.
GAS_FLOOR = float(os.environ.get("ROLL_GAS_FLOOR", "0.75"))

#: The collateral token. On Arc this is the native USDC predeploy — the default
#: — but it is the venue's *configured* token, not a law of the universe, so it
#: is overridable. That is what lets this script be exercised against a real
#: deployment on anvil (see tests/test_futures_roll_onchain.py) instead of only
#: ever being run in production for the first time, which is what it was doing.
USDC_PREDEPLOY = os.environ.get(
    "ROLL_USDC_ADDRESS", "0x3600000000000000000000000000000000000000"
)


def _check(ok: bool, label: str) -> bool:
    print(f"  {'✓' if ok else '✗'} {label}")
    return ok


def main() -> None:
    s = get_settings()
    # An explicit key still wins (anvil, offline); otherwise the maker ROLE's
    # Circle wallet. Deliberately NOT `or s.poster_private_key`: that ambient
    # fallback is why every venue script signed as a raw EOA even on a host with
    # full Circle credentials — there is always a key in .env.
    maker_key = os.environ.get("MAKER_PRIVATE_KEY", "")
    signer = build_role_signer("maker", s, private_key=maker_key or None)
    if not (s.futures_address and signer):
        print("set ACR_FUTURES_ADDRESS, and either ACR_CIRCLE_MAKER_WALLET_ID "
              "(+ ACR_CIRCLE_API_KEY) or MAKER_PRIVATE_KEY")
        sys.exit(1)

    # `openSeries` is onlyOwner, and OWNERSHIP IS A DIFFERENT JOB FROM MARKET
    # MAKING. The owner is governance — it decides a series may exist — and it
    # acts rarely, on a human's initiative. The maker is operations: it stands
    # behind the book and posts collateral every roll. Collapsing them is how
    # one credential ended up being four jobs. So this script signs `openSeries`
    # as the OWNER and everything else as the MAKER; when they are the same
    # address (the pre-migration state) nothing changes.
    owner_key = (
        os.environ.get("VENUE_OWNER_PRIVATE_KEY", "")
        or os.environ.get("MAKER_PRIVATE_KEY", "")
    )
    owner_signer = build_role_signer("owner", s, private_key=owner_key or None)
    if owner_signer is None:
        # Ownership migrated from the deploy EOA to the MAKER's Circle wallet on
        # 2026-08-03 — that is what lets the keeper roll unattended, and
        # keeper.py already opens with one maker signer. So the maker is the
        # right fallback.
        #
        # It used to fall back to `s.poster_private_key`, a RAW key for the
        # retired deploy EOA. That is no longer the owner, so `openSeries`
        # reverted with "not owner" — and the failure was the good outcome: the
        # bad one is a venue operation signed by the very key the migration
        # existed to retire.
        owner_signer = build_role_signer("maker", s)
        if owner_signer is not None:
            print("  · no owner role configured — signing as the maker, which owns the venue")
    if owner_signer is None:
        print("no owner signer — set ACR_CIRCLE_OWNER_WALLET_ID (or ACR_CIRCLE_MAKER_WALLET_ID)")
        sys.exit(1)

    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(s.arc_rpc_url, request_kwargs={"timeout": 25}))
    fc = FuturesClient(rpc_url=s.arc_rpc_url, futures_address=s.futures_address,
                       signer=signer)
    owner_fc = FuturesClient(rpc_url=s.arc_rpc_url, futures_address=s.futures_address,
                             signer=owner_signer)
    # Resolving a Circle wallet's address is a network call, so do it once.
    maker_address = signer.address

    chain_id = _rpc_retry(lambda: w3.eth.chain_id)
    if not _check(chain_id == s.arc_chain_id, f"chain id {chain_id} == {s.arc_chain_id}"):
        sys.exit(1)

    def _how(sg) -> str:
        return "Circle custody" if type(sg).__name__ == "CircleWalletSigner" else "local key"

    # ASK THE CHAIN who owns the venue, rather than trusting the role that
    # claims to. `build_role_signer("owner", …)` falls back to the ambient
    # poster key when no owner wallet is configured — correct before the
    # 2026-08-03 handover, and silently wrong after it, because that key is the
    # retired deploy EOA. The symptom was `openSeries` reverting "not owner"
    # after the budget guard had already passed. Ownership is a fact on-chain;
    # read it and pick the signer that matches.
    _OWNER_ABI = [{"type": "function", "name": "owner", "stateMutability": "view",
                   "inputs": [], "outputs": [{"name": "", "type": "address"}]}]
    try:
        on_chain_owner = _rpc_retry(
            w3.eth.contract(
                address=Web3.to_checksum_address(s.futures_address), abi=_OWNER_ABI
            ).functions.owner().call
        )
        if str(on_chain_owner).lower() != owner_signer.address.lower():
            if str(on_chain_owner).lower() == maker_address.lower():
                owner_signer = signer  # the maker owns it — sign with the maker
                owner_fc = fc
                print("  · the venue is owned by the MAKER — signing openSeries as the maker")
            else:
                print(f"  ✗ the venue is owned by {on_chain_owner}, and no configured "
                      "signer holds it — set ACR_CIRCLE_OWNER_WALLET_ID")
                sys.exit(1)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — a verdict beats a traceback
        print(f"  ! could not read the venue owner ({str(exc)[:60]}) — continuing")

    print(f"  · venue {s.futures_address}")
    print(f"  · maker {maker_address} ({_how(signer)})")
    print(f"  · owner {owner_signer.address} ({_how(owner_signer)})")

    # 1) Idempotence — a healthy series with life left needs no roll.
    now = int(_rpc_retry(lambda: w3.eth.get_block("latest"))["timestamp"])
    for existing in fc.read_all_series():
        if existing["index_id"] != INDEX or existing["settled"]:
            continue
        hours_left = (existing["expiry_ts"] - now) / 3600
        # A THROTTLED READ IS NOT AN UNFUNDED MAKER. `or 0` used to flatten a
        # failed read into "NONE", and the response is to open a whole new
        # series and post collateral to it. That fired for real: series 1 had
        # 147.9 HOURS of life left and a funded maker, and a 429 made this
        # roll anyway — spending 1.50 USDC on a successor nothing needed.
        funded_read = collateral_or_none(fc, existing["series_id"], maker_address)
        if funded_read is None:
            print(f"  ⏹ could not read the maker's collateral on series "
                  f"{existing['series_id']} after retries — refusing to roll on a "
                  "guess; the next run will see it")
            sys.exit(0)
        funded = funded_read > 0
        if hours_left > MIN_LIFE_H and funded:
            print(f"  ↩ series {existing['series_id']} has {hours_left:.1f}h left and is "
                  f"collateralized — nothing to roll")
            sys.exit(0)
        print(f"  · series {existing['series_id']}: {hours_left:.1f}h left, "
              f"maker collateral {'yes' if funded else 'NONE'} → rolling")

    # 2) Budget guard BEFORE any write — collateral and gas come from one balance.
    native = _rpc_retry(w3.eth.get_balance, Web3.to_checksum_address(maker_address)) / 1e18
    if not _check(
        native - COLLATERAL >= GAS_FLOOR,
        f"maker holds {native:.3f} USDC; posting {COLLATERAL:.2f} leaves "
        f"{native - COLLATERAL:.3f} ≥ {GAS_FLOOR} for gas",
    ):
        print("    top the maker up, or lower ROLL_COLLATERAL")
        sys.exit(1)

    # 3) Open. onlyOwner, and the contract itself requires a live oracle value,
    #    so a dead feed fails here rather than halfway through.
    expiry = now + int(EXPIRY_DAYS * 86400)
    owner_fc.open_series(INDEX, expiry, MULT, maker_address)
    sid = len(fc.read_all_series()) - 1  # re-read; don't trust a receipt return
    print(f"  ① opened series {sid} ({INDEX}, mult {MULT}, expiry +{EXPIRY_DAYS:g}d)")

    # 4) Allowance, then collateral. The approve goes through the SIGNER like
    #    every other write — it used to be hand-built and signed with
    #    eth_account, which is why the whole venue was pinned to a raw key even
    #    on a host with full Circle credentials: collateral cannot be posted
    #    without an allowance, so that one omission decided everything.
    allowance = fc.allowance_units(USDC_PREDEPLOY, maker_address)
    if allowance is None:
        print("  ⏹ could not read the venue's allowance — refusing to roll on a guess")
        sys.exit(0)
    if allowance < int(COLLATERAL * 1_000_000):
        fc.approve_venue(USDC_PREDEPLOY)
        print("  ② re-approved the venue on the USDC predeploy")
        time.sleep(2)  # pace the throttled Arc RPC between writes

    fc.post_collateral(sid, COLLATERAL)

    # 5) The assertion that makes this script worth having.
    posted = fc.collateral_of(sid, maker_address) or 0.0
    if not _check(posted > 0, f"maker collateral on series {sid}: {posted:.2f} USDC"):
        print("    the series is OPEN but UNCOLLATERALIZED — the desk would revert on "
              "first contact. Fix the collateral step before advertising this venue.")
        sys.exit(1)

    desk = fc.read_desk(INDEX) or {}
    print(f"  ③ desk reads back: maker inventory {desk.get('maker_inventory', 0):+.2f}, "
          f"traders {desk.get('trader_count', 0)}, expiry +{EXPIRY_DAYS:g}d")
    print("roll: DONE")


if __name__ == "__main__":
    main()
