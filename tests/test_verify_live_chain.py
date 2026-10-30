"""A testnet audit must look at the testnet terminal.

The terminal serves both chains from one host and picks by the `chain` query
key, defaulting to mainnet. `verify_live.py` called it bare, so pointing the
audit at the testnet press still graded MAINNET's terminal: on 2026-10-14 a
testnet run failed "/api/futures -> 0 desk(s)" and "0 fill(s)" on mainnet's
empty venue while `?chain=testnet` answered for the testnet one.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import verify_live  # noqa: E402


def test_a_testnet_run_asks_the_terminal_for_testnet() -> None:
    url = verify_live.terminal_url("/api/futures", verify_live.TESTNET_CHAIN_ID)
    assert url == f"{verify_live.TERMINAL}/api/futures?chain=testnet"


def test_an_existing_query_string_is_extended_not_replaced() -> None:
    url = verify_live.terminal_url("/api/wallet/balances?address=0xab&price=0.001", 5042002)
    assert url.endswith("/api/wallet/balances?address=0xab&price=0.001&chain=testnet")


def test_mainnet_and_local_runs_keep_the_bare_url() -> None:
    for chain in (5042, 31337):
        assert verify_live.terminal_url("/api/futures", chain) == f"{verify_live.TERMINAL}/api/futures"


def test_no_terminal_call_bypasses_the_chain() -> None:
    """Every request to the terminal goes through `terminal_url`; a bare
    f"{TERMINAL}…" call is the bug this file exists for."""
    source = (ROOT / "scripts" / "verify_live.py").read_text(encoding="utf-8")
    bare = [m.group(0) for m in re.finditer(r"(?:get|post)\(f\"\{TERMINAL\}[^\"]*\"", source)]
    assert not bare, f"terminal calls that ignore the audited chain: {bare}"


def test_an_empty_tape_says_whether_it_was_read() -> None:
    # `0 fill(s)` named neither cause; the press's freshness block tells an
    # unread tape (failing RPC reads) from an empty one (nothing traded).
    assert "not managed to READ" in verify_live.tape_cause({"tape_age_s": None})
    assert "no fills within reach" in verify_live.tape_cause({"tape_age_s": 12.0})
    assert verify_live.tape_cause(None) == ""  # an older press: say nothing rather than guess
