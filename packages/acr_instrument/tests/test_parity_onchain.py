"""Cross-implementation parity with the on-chain ``ACRFutures`` contract.

The Solidity ``_applyFill`` ports ``Position.apply_fill`` byte-for-byte so the
Python maker and the chain never disagree — the same discipline ``ACROracle``
uses for ``printDigest``. This test runs the SAME fill vector as the Solidity
twin ``contracts/test/ACRFutures.t.sol::test_Parity_MatchesPythonPosition`` and
asserts the identical end state. If either implementation drifts, one of these
two tests goes red.
"""

from __future__ import annotations

from acr_instrument import Position

# (qty contracts, mark) — clean divisions so float (Python) and fixed-point
# (Solidity, 1e18) agree exactly. Mirrors the on-chain vector.
FILLS = [(2.0, 0.50), (2.0, 0.60), (-1.0, 0.70), (-5.0, 0.40)]


def test_apply_fill_vector_matches_onchain():
    pos = Position()
    for qty, price in FILLS:
        pos.apply_fill(qty, price)
    # Identical to the Solidity assertions (WAD-descaled): contracts -2,
    # avg 0.40, realized -0.30.
    assert pos.contracts == -2.0
    assert abs(pos.avg_price - 0.40) < 1e-12
    assert abs(pos.realized_pnl - (-0.30)) < 1e-12
