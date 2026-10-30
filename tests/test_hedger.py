"""The autonomous hedger's two arithmetic decisions.

Hermetic: no chain, no Circle, no credentials — these are the pure predicates
the agent spends money on, so they are the part worth pinning. The rest of
`hedger.py` is I/O around them.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

from hedger import _find_ref, _settlement_ref, margin_bound, topup_for  # noqa: E402

#: The live numbers on 2026-08-04: mark 0.4924, multiplier 10, MARGIN_BPS 2000.
PER_CONTRACT = 0.4924 * 10 * 0.2
SAFETY = 0.90


def _cap(collateral: float) -> float:
    return SAFETY * collateral / PER_CONTRACT


def test_our_own_margin_is_told_apart_from_a_full_book():
    """The distinction the agent spends money on.

    It sat at 1.82 contracts on 2.00 USDC of its own collateral: taker cap
    1.817, so max_buy was exactly 0.00 while the maker had depth to spare. It
    reported "the book has no room" and stopped — tick after tick, short of a
    mandate a few cents of margin would have reached.
    """
    t_cap = _cap(2.0)  # ~1.817
    m_cap = _cap(5.58)  # the maker's side, much deeper
    assert margin_bound(+0.18, t_cap, 1.82, m_cap, -2.82) is True


def test_a_full_book_is_not_fixed_by_buying_margin():
    """The expensive direction to get wrong: margin that changes nothing."""
    t_cap = _cap(50.0)  # we are richly margined
    m_cap = _cap(0.05)  # the maker is not
    assert margin_bound(+1.0, t_cap, 0.0, m_cap, 0.0) is False


def test_the_sell_side_uses_the_mirrored_terms():
    # A sell is capped by (t_cap + position) on our side and (m_cap − maker_inv)
    # on theirs — the signs flip, and a predicate that ignored that would give
    # the opposite answer on exactly half the trades.
    t_cap, m_cap = _cap(2.0), _cap(5.58)
    assert margin_bound(-0.50, t_cap, -1.80, m_cap, 0.0) is True
    assert margin_bound(-0.50, _cap(50.0), 0.0, _cap(0.05), 0.0) is False


def test_a_top_up_buys_exactly_the_room_the_trade_needs():
    # 1.82 + 0.18 = 2.00 contracts of cap, at 0.9848 USDC of margin each,
    # quoted at 90% safety → ~2.19 USDC of collateral, so ~0.19 more than the
    # 2.00 already posted.
    add = topup_for(+0.18, 1.82, PER_CONTRACT, SAFETY, 2.0, 0.50, 3.0)
    assert 0.15 < add <= 0.30, add
    # And it genuinely clears: the new cap must cover position + want.
    assert _cap(2.0 + add) >= 1.82 + 0.18


def test_a_top_up_is_bounded_twice():
    """Per tick AND in total. An agent that can post collateral can spend, so
    the ceiling lives with the arithmetic rather than in the caller's head."""
    # A huge gap wants far more than one tick may add.
    assert topup_for(+50.0, 0.0, PER_CONTRACT, SAFETY, 2.0, 0.50, 3.0) == 0.50
    # Already at the lifetime cap: nothing may be added, at any size of gap.
    assert topup_for(+50.0, 0.0, PER_CONTRACT, SAFETY, 3.0, 0.50, 3.0) == 0.0
    # Near the cap, only the remainder is allowed.
    assert topup_for(+50.0, 0.0, PER_CONTRACT, SAFETY, 2.8, 0.50, 3.0) == 0.20


def test_nothing_is_posted_when_nothing_is_needed():
    # Richly margined already — a top-up here would be money for no room.
    assert topup_for(+0.18, 1.82, PER_CONTRACT, SAFETY, 10.0, 0.50, 20.0) == 0.0
    # And an unreadable/absent mark must not produce a confident number.
    assert topup_for(+0.18, 1.82, 0.0, SAFETY, 2.0, 0.50, 3.0) == 0.0


def test_the_deployed_mandate_is_the_scripts_default():
    """Two files, two defaults, one agent.

    The script defaulted to 1.0 while the deployed service advertised 2.0, so
    `make hedger` with only ACR_HEDGER_ADDRESS exported would have SOLD 0.82 —
    the opposite of the mandate on the site a judge reads. They must move
    together, which is why this pins both against one number rather than
    trusting two files to be edited at once.
    """
    import hedger
    from index_api.hedger import TARGET_CONTRACTS

    assert hedger.TARGET == 2.5
    assert TARGET_CONTRACTS == hedger.TARGET


def test_a_settlement_ref_is_found_at_whatever_depth_it_arrives():
    """`print_tx` was null on all seven of the log's first live lines, including
    three whose own notes say the payment succeeded.

    The old lookup read three keys at ONE depth, off a body from which the
    caller had already stripped a `data` envelope — so a reference one level
    down was invisible, and every run wrote another silent null. This is the
    pure half of that fix; the widened search itself can only be proven by a
    paid run, and `HEDGER_DRY_RUN=1` returns before it.
    """
    ref = "b2dd87be-d626-41f5-80e4-87b34fb1da54"
    assert _find_ref({"settlementRef": ref}) == ref
    # The shape the old code could not see.
    assert _find_ref({"payment": {"settlement": {"id": ref}}}) == ref
    # And inside a list, which a pay response uses for batched authorizations.
    assert _find_ref({"settlements": [{"transactionId": ref}]}) == ref


def test_a_ref_that_is_not_a_reference_is_refused():
    """A bool under `id` is a flag and a dict under `transactionId` is some
    other object. Writing either into `print_tx` would replace a null with
    something worse: a field that looks populated and resolves to nothing."""
    assert _find_ref({"id": True}) is None
    assert _find_ref({"id": {}}) is None
    assert _find_ref({"id": "   "}) is None
    assert _find_ref({}) is None
    assert _find_ref(None) is None
    # Depth-bounded, so a large payload cannot turn a log line into a tree walk.
    deep = {"a": {"b": {"c": {"d": {"e": {"id": "too-deep"}}}}}}
    assert _find_ref(deep) is None


def test_the_logged_ref_is_the_one_the_public_tape_publishes():
    """`data.payment.receipt` is the x402 X-PAYMENT-RESPONSE header verbatim:
    base64 over {success, transaction, network, payer}.

    Storing that blob would be worse than storing nothing — it looks like a
    reference and matches no receipt anyone can look up. The `transaction`
    inside it is the real one. Measured against production on 2026-08-08: this
    exact payload decodes to the `tx_ref` the seller's own /marketplace/receipts
    published for that same payment, so the agent's log and the public tape can
    finally name the same thing.
    """
    import base64
    import json

    uuid = "b0c1be36-6283-4660-af3e-2ad0a8bdfe33"
    header = base64.b64encode(
        json.dumps({"success": True, "transaction": uuid,
                    "network": "eip155:5042002", "payer": "0x71e1"}).encode()
    ).decode()
    assert _settlement_ref(header) == uuid
    # A plain reference passes through untouched, so a CLI that stops wrapping
    # it does not silently start logging None.
    assert _settlement_ref(uuid) == uuid
    assert _settlement_ref(None) is None
    # Base64 that is not JSON is not a wrapper; keep what we were given rather
    # than inventing a decode.
    assert _settlement_ref(base64.b64encode(b"not json").decode()) is not None
