"""The human-denominated bound, and the constant it refuses to hide.

The pinned-value test at the bottom is the load-bearing one. `anchors-check`
validates that the basket is internally coherent, but it prices C_human FROM the
basket, so editing the floor moves both sides together and the check passes — a
parity test comparing a file to itself cannot fail in the direction that matters.
This file is the independent witness: change the floor and this goes red, which
forces the edit to be deliberate rather than silent. That is the failure mode
`reference_level` actually had.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from acr_estimator.bound import manipulation_bound
from acr_estimator.human_caps import (
    CAP_HUMAN,
    CAP_UNVERIFIED,
    human_bound,
    humans_required,
    load_cost_per_human,
)

BASKET = Path(__file__).resolve().parents[3] / "anchors" / "_basket" / "C-HUMAN.json"


def _book(n: int = 200) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(7)
    prices = rng.normal(1.0, 0.02, n)
    return prices, np.full(n, 50.0)


def _bound(**over):
    prices, weights = _book()
    kw = dict(alpha=0.1, fee_bps=1.0, fee_flat=0.001, raw_total=float(weights.sum()))
    kw.update(over)
    return prices, weights, kw


# --- the count ----------------------------------------------------------------


def test_moving_the_print_needs_more_than_one_human():
    """The headline claim. A cap that one actor could satisfy alone is not a cap."""
    prices, weights, kw = _bound()
    people, _ = humans_required(prices, weights, **kw)
    assert people >= 1


def test_a_looser_human_cap_needs_fewer_humans():
    """Monotonicity — and the economic point: proving you are a person buys
    headroom, so each verified human carries more before being capped."""
    prices, weights, kw = _bound()
    tight, _ = humans_required(prices, weights, cap_human=0.02, **kw)
    loose, _ = humans_required(prices, weights, cap_human=0.20, **kw)
    assert tight >= loose


def test_verified_flow_is_capped_looser_than_unverified():
    assert CAP_HUMAN > CAP_UNVERIFIED


def test_a_human_is_one_capped_actor_not_a_fabricated_community():
    """No evade-size multiplier on the human path.

    Under the wallet cap a cluster is a funding community an attacker must
    fabricate, so identities multiply by SYBIL_MAX_SIZE+1. A verified human needs
    no disguise. Carrying that multiplier over would inflate the headline ~40x —
    an error that flatters, and therefore would not get questioned.
    """
    prices, weights, kw = _bound()
    people, mb = humans_required(prices, weights, **kw)
    assert people == mb.sybil_clusters_required
    assert mb.min_identities == people  # evade size 1, so they coincide


# --- the price ----------------------------------------------------------------


def test_the_cost_comes_from_the_anchor_not_a_constant():
    usd, status, is_floor = load_cost_per_human()
    basket = json.loads(BASKET.read_text())
    assert usd == basket["rule"]["floor_usd"]
    assert status == basket["status"]
    assert is_floor is True


def test_a_missing_basket_is_an_error_not_a_fallback(tmp_path):
    """Falling back to a constant is precisely what anchors/ exists to prevent."""
    with pytest.raises(FileNotFoundError):
        load_cost_per_human(tmp_path / "nope.json")


def test_the_bound_never_undercuts_the_wallet_bound():
    """`ACROracleV2` enforces `humanAdjustedBound >= attackCostPerBp`, and it is
    enforcing something true: buying people cannot be cheaper than buying
    wallets. A post that violated it would revert on chain."""
    prices, weights, kw = _bound()
    wallet = manipulation_bound(
        prices, weights, kw["alpha"], kw["fee_bps"], kw["fee_flat"],
        cluster_cap=CAP_UNVERIFIED, raw_total=kw["raw_total"],
    )
    hb = human_bound(prices, weights, wallet_cost_per_bp=wallet.cost_per_bp, **kw)
    assert hb is not None
    assert hb.cost_usdc >= wallet.cost_per_bp


def test_the_bound_declares_itself_a_lower_bound():
    """Nothing downstream may present this as measured by accident."""
    prices, weights, kw = _bound()
    hb = human_bound(prices, weights, wallet_cost_per_bp=1.0, **kw)
    assert hb.is_lower_bound is True
    assert hb.basket_status == "unsourced"


# --- the bless ----------------------------------------------------------------


def test_the_floor_is_pinned_so_it_cannot_be_edited_silently():
    """THE INDEPENDENT WITNESS. Read the module docstring before changing this.

    `anchors-check` prices C_human from the basket, so editing the floor moves
    the check's expectation with it and passes green. This assertion lives
    outside that file and therefore fails, which is the only thing that makes the
    edit deliberate. If you are changing the floor on purpose: add cited rows to
    the basket, change `aggregate` away from `floor`, and update this number in
    the same commit.
    """
    usd, status, _ = load_cost_per_human()
    assert usd == 10.0, "C_human floor changed — re-anchor deliberately, see the docstring"
    assert status == "unsourced", "basket status changed — has it been sourced?"
