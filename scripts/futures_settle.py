#!/usr/bin/env python
"""Cash-settle expired futures series against the oracle print.

``settle`` is permissionless — anyone can call it — but until this script
existed nothing in the repo ever did on Arc, so an expired series stayed open
forever and every trader's collateral stayed pinned behind its margin
requirement. Settlement is what flattens positions and frees the whole cleared
balance for withdrawal.

Two generations of venue exist, and this script serves both:

* **Current** (`SETTLE_GRACE()` answers): `settle` clears at the FIRST print the
  chain saw at or after expiry, provided it landed within ``MAX_SETTLE_AGE`` of
  expiry — the feed was alive when it mattered. If it was not, once
  ``SETTLE_GRACE`` has passed, `settleStale` clears at the best print that
  exists, whatever its age. Nothing stays locked past the grace.
* **Pre-escape-hatch** (the venue deployed before it): `settle` needs the LATEST
  print to be under ``MAX_SETTLE_AGE`` old right now. That window reopens with
  every print, so a quiet feed means "run the poster, then re-run" — and if the
  press never returns, that generation has no way out. It is the reason the
  current one exists (docs/SECURITY-AUDIT.md, C3).

    MAKER_PRIVATE_KEY=0x… uv run python scripts/futures_settle.py  # == make futures-settle
    SETTLE_SERIES=0 …                                              # just one series
"""

from __future__ import annotations

import os
import sys
import time

from acr_core import get_settings
from acr_oracle_client import FuturesClient, OracleClient, build_role_signer
from acr_oracle_client.futures import _rpc_retry

#: Mirrors ACRFutures.MAX_SETTLE_AGE. Read from the chain when possible.
DEFAULT_MAX_SETTLE_AGE = 7200
ONLY = os.environ.get("SETTLE_SERIES", "")

_AGE_ABI = [{"type": "function", "name": "MAX_SETTLE_AGE", "stateMutability": "view",
             "inputs": [], "outputs": [{"name": "", "type": "uint64"}]}]


def main() -> None:
    s = get_settings()
    # settle() is permissionless — any funded signer can call it. Uses the maker
    # role's Circle wallet by default so the keeper needs no private key; an
    # explicit MAKER_PRIVATE_KEY still wins for anvil and offline runs.
    key = os.environ.get("MAKER_PRIVATE_KEY", "")
    signer = build_role_signer("maker", s, private_key=key or None)
    if not (s.futures_address and signer):
        print("set ACR_FUTURES_ADDRESS, and either ACR_CIRCLE_MAKER_WALLET_ID "
              "(+ ACR_CIRCLE_API_KEY) or MAKER_PRIVATE_KEY (any funded signer works)")
        sys.exit(1)

    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(s.arc_rpc_url, request_kwargs={"timeout": 25}))
    fc = FuturesClient(rpc_url=s.arc_rpc_url, futures_address=s.futures_address, signer=signer)
    oracle = OracleClient(rpc_url=s.arc_rpc_url, oracle_address=s.oracle_address or None)

    try:
        c = w3.eth.contract(address=Web3.to_checksum_address(s.futures_address), abi=_AGE_ABI)
        max_age = int(_rpc_retry(c.functions.MAX_SETTLE_AGE().call))
    except Exception:
        max_age = DEFAULT_MAX_SETTLE_AGE

    now = int(_rpc_retry(lambda: w3.eth.get_block("latest"))["timestamp"])
    series = fc.read_all_series()
    if ONLY:
        series = [x for x in series if str(x["series_id"]) == ONLY]

    due = [x for x in series if not x["settled"] and x["expiry_ts"] <= now]
    if not due:
        print("  ↩ nothing expired and unsettled")
        sys.exit(0)
    grace = fc.settle_grace()  # None on the pre-escape-hatch venue
    print("  venue: " + (f"current — first-print rule, escape hatch after {grace / 86400:.0f}d"
                         if grace else "pre-escape-hatch — latest-print rule"))
    stuck = 0
    for x in due:
        sid, iid, exp = x["series_id"], x["index_id"], int(x["expiry_ts"])
        print(f"\n  series {sid} ({iid}) — expired {(now - exp) / 3600:.1f}h ago")
        if grace is not None:
            first = oracle.read_first_print_posted_at_or_after(iid, exp)
            lag = (first["posted_at"] - exp) if first else None
            if first and lag <= max_age:
                fc.settle(sid)
                how = f"first post-expiry print, {lag / 60:.0f}m after expiry"
            elif now >= exp + grace:
                fc.settle_stale(sid)
                how = "escape hatch — " + (f"post-expiry print {lag / 3600:.1f}h late"
                                           if first else "last pre-expiry print")
            else:
                when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(exp + grace))
                why = ("no print since expiry" if not first else
                       f"first post-expiry print landed {lag / 3600:.1f}h after it (> {max_age / 3600:.0f}h)")
                print(f"    ✗ {why}. `settle` would revert; `settleStale` opens {when}.")
                stuck += 1
                continue
        else:
            print_ = oracle.read_latest(iid)
            age = (time.time() - print_["posted_at"]) if print_ else None
            if age is None or age > max_age:
                shown = "no print" if age is None else f"{age / 60:.0f}m old"
                print(f"    ✗ the {iid} print is {shown}; settle needs one under "
                      f"{max_age / 60:.0f}m. NOT settling — this would revert 'stale print'.")
                print("      The window reopens on the next print; run the poster, then re-run.")
                stuck += 1
                continue
            fc.settle(sid)
            how = f"latest print, {age / 60:.0f}m old"
        settled = next(v for v in fc.read_all_series() if v["series_id"] == sid)
        print(f"    ✓ settled @ {settled['settlement_price']:.5f} ({how})")

        # The withdrawal manifest: who is owed what, now that positions are flat.
        n = int(_rpc_retry(fc._contract().functions.traderCount(sid).call))
        print(f"    cleared balances ({n} traders) — each can now withdraw in full:")
        for i in range(n):
            t = _rpc_retry(fc._contract().functions.traderAt(sid, i).call)
            bal = fc.collateral_of(sid, t) or 0.0
            print(f"      {t}  {bal:.6f} USDC")

    print("\nsettle:", "INCOMPLETE (stale print)" if stuck else "DONE")
    sys.exit(1 if stuck else 0)


if __name__ == "__main__":
    main()
