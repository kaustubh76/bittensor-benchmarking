"""The operator running without being asked, and every reason it must not.

An agent somebody has to trigger is a tool. This is the loop that makes
"obligations settled without a human touching them" a thing the record shows
rather than a thing the design permits — and because it spends money on a timer,
almost every test here is about something it REFUSES to do.

The rules come from `keeper.py`, which is the venue's version of the same
problem: never take the press down, do nothing without custody, and let a
cooldown bound every write. Plus one of this loop's own — off by default,
because a money loop that is armed unless you remember to disarm it is not a
guardrail.
"""

from __future__ import annotations

import pytest
from index_api import operator_keeper as ok


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Module state is global by design (one process, no leader election), so
    each test gets it empty."""
    ok._last.clear()
    monkeypatch.delenv("ACR_OPERATOR_AUTORUN", raising=False)
    yield
    ok._last.clear()


# --- armed, or not ----------------------------------------------------------


def test_it_is_off_unless_somebody_says_otherwise():
    """The default every checkout, laptop and CI run gets."""
    assert ok.mode() == "off"
    assert ok.enabled() is False
    assert ok.tick_once() is None, "a disarmed loop must not even look"


def test_a_truthy_flag_means_dry_and_never_live(monkeypatch):
    """The one thing a vague `=1` must not do is start paying. Someone arming
    this in a hurry gets the safe half of what they asked for."""
    for raw in ("1", "true", "yes", "on"):
        monkeypatch.setenv("ACR_OPERATOR_AUTORUN", raw)
        assert ok.mode() == "dry", raw


def test_an_unrecognised_mode_is_off(monkeypatch):
    """Fail-safe direction. A typo in the one variable that authorises spending
    should stop the spending, not start it."""
    monkeypatch.setenv("ACR_OPERATOR_AUTORUN", "ture")
    assert ok.mode() == "off"


def test_live_is_spelled_out_in_full(monkeypatch):
    monkeypatch.setenv("ACR_OPERATOR_AUTORUN", "live")
    assert ok.mode() == "live" and ok.enabled() is True


# --- the clock --------------------------------------------------------------


def test_the_interval_has_a_floor_no_env_var_can_lower(monkeypatch):
    """`EVERY_S` comes from the environment and this loop spends money, so a
    typo must not be able to turn it into a tight loop against a PolicyWallet.
    The on-chain cap would still hold — but by being exhausted, which is not the
    same as being respected."""
    monkeypatch.setattr(ok, "EVERY_S", 1.0)
    monkeypatch.setenv("ACR_OPERATOR_AUTORUN", "live")
    assert ok.sleep_s() == ok.MIN_EVERY_S


def test_a_cooldown_bounds_every_pass(monkeypatch):
    """Rule three from `keeper.py`. Two passes in one period would read the same
    tape and reach the same decisions."""
    monkeypatch.setenv("ACR_OPERATOR_AUTORUN", "dry")
    monkeypatch.setattr(ok, "_pass", lambda now: "did a thing")
    assert ok.tick_once() == "did a thing"
    assert ok.tick_once() is None, "the second tick is inside the cooldown"
    assert ok.tick_once(force=True) == "did a thing", "and force is the operator's override"


def test_a_pass_already_running_is_not_joined_by_another(monkeypatch):
    """Non-blocking, like `keeper.mirror_once`. Two passes racing on one wallet
    is how a nonce collides — and a blocking lock would instead pile the press's
    threads up behind a slow chain."""
    monkeypatch.setenv("ACR_OPERATOR_AUTORUN", "dry")
    ok._lock.acquire()
    try:
        assert ok.tick_once(force=True) is None
    finally:
        ok._lock.release()


# --- it can never take the press down ---------------------------------------


def test_a_failing_pass_is_a_verdict_not_a_traceback(monkeypatch):
    """Rule one. The loop runs inside the press's lifespan; an exception that
    escaped would be an unhandled error in a background task."""
    monkeypatch.setenv("ACR_OPERATOR_AUTORUN", "dry")

    def boom(_now):
        raise RuntimeError("the chain said 429")

    monkeypatch.setattr(ok, "_pass", boom)
    verdict = ok.tick_once(force=True)
    assert verdict and verdict.startswith("failed:")
    assert "429" in verdict, "and it says what went wrong"


def test_the_tick_records_itself(monkeypatch):
    """Not the caller. A chore whose caller is responsible for saying it ran
    reads as dead the first time a caller forgets — and that is not
    hypothetical: the live press reports `checked_at: null` for all three keeper
    chores, and "nobody recorded it" is one of the candidate explanations."""
    monkeypatch.setenv("ACR_OPERATOR_AUTORUN", "dry")
    monkeypatch.setattr(ok, "_pass", lambda now: "a verdict")
    ok.tick_once(force=True)
    st = ok.status()
    assert st["verdict"] == "a verdict"
    assert st["checked_at"] is not None and st["checked_age_s"] is not None


def test_status_tells_on_cooldown_from_never_fired(monkeypatch):
    """`keeper._chore_status` gives both clocks for this reason: the healthy
    majority of ticks is a cooldown, and a surface that showed only one could
    not tell a resting loop from a dead one."""
    assert ok.status()["fired_at"] is None
    assert ok.status()["next_due_s"] == 0.0

    monkeypatch.setenv("ACR_OPERATOR_AUTORUN", "dry")
    monkeypatch.setattr(ok, "_pass", lambda now: ok._last.__setitem__("fired_at", now) or "ran")
    ok.tick_once(force=True)
    st = ok.status()
    assert st["fired_at"] is not None
    assert 0 < st["next_due_s"] <= max(ok.EVERY_S, ok.MIN_EVERY_S)


# --- what a pass actually does ----------------------------------------------


def test_an_empty_tape_is_refused_rather_than_read_as_caught_up(monkeypatch):
    """THE BUG THIS CAUGHT IN ITSELF. The facilitator's ring rehydrates from
    `ACR_RECEIPT_ARCHIVE_PATH`; unset — a fresh local process — it starts empty,
    and the first version of this reported "nothing has settled since the last
    bill paid". That is a claim about the books made without reading them.
    Billing from a tape we cannot see is the one thing an operator must not do.
    """
    monkeypatch.setenv("ACR_OPERATOR_AUTORUN", "dry")
    monkeypatch.setattr(ok, "_businesses", lambda: [object()])
    import index_api.marketplace as mkt

    monkeypatch.setattr(mkt, "build_receipts", lambda _fac: {"receipts": []})
    verdict = ok.tick_once(force=True)
    assert "tape is empty" in verdict
    assert "nothing has settled" not in verdict, "the two are different claims"


def test_no_business_with_a_wallet_is_said_plainly(monkeypatch):
    monkeypatch.setenv("ACR_OPERATOR_AUTORUN", "live")
    monkeypatch.setattr(ok, "_businesses", lambda: [])
    assert "no business on this chain" in ok.tick_once(force=True)


def test_only_businesses_on_this_press_chain_are_operated(monkeypatch):
    """`chain` is a label, and a label is what went wrong when the
    testnet→mainnet switch left a mislabelled bundle behind. This is the cheap
    half of the guard; `wallet_status()` does an `eth_getCode` at the moment of
    payment, which is the half a label cannot fake."""
    from index_api import businesses as reg
    from index_api.businesses import Business

    def _b(slug, chain, wallet="0x" + "a7" * 20):
        return Business(slug=slug, treasury="0x" + "11" * 20, policy_wallet=wallet, chain=chain)

    monkeypatch.setattr(
        reg, "real",
        lambda *_a, **_k: (_b("here", "testnet"), _b("elsewhere", "mainnet"), _b("broke", "testnet", "")),
    )
    slugs = [b.slug for b in ok._businesses()]
    assert slugs == ["here"], "wrong chain excluded, and no wallet means nothing to spend from"


def test_the_loop_never_settles_an_escalation_itself():
    """The one thing a human is for. `run_obligation` is the only path a pass
    takes, and it cannot reach `spend_as_owner` — that stays behind the
    token-gated POST /ops/actions. A loop approving its own escalations would
    not be more autonomous; it would make `traction.py`'s "settled without a
    human touching them" figure, which keys on `actor`, mean nothing.
    """
    import ast
    import inspect

    # Parsed, not grepped: this module's own docstring names `spend_as_owner` to
    # explain why it is absent, and a substring scan read that as the call.
    tree = ast.parse(inspect.getsource(ok))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "spend_as_owner" not in called
    assert "spendAsOwner" not in called
    assert "run_obligation" in called, "and the one path it does take is the agent's own"


# --- it has to be able to pay, and must not pretend it did ------------------


class _Biz:
    slug = "payer"
    treasury = "0x" + "11" * 20
    policy_wallet = "0x" + "a7" * 20
    chain = "testnet"
    categories = ("machine-services",)


def _tape_row(at: float = 1_000.0) -> dict:
    return {
        "payer": _Biz.treasury,
        "seller": "0x" + "ab" * 20,
        "resource": "/compute/x",
        "amount_usdc": 0.5,
        "quantity": 100.0,
        "settled_at": at,
    }


#: One invoice, the agoranomoi case: fifty seats billed, twelve opened. Carries
#: the three fields the tape feeder cannot supply — `due_at`, `invoice_ref` and
#: a `used_quantity` measured by somebody other than the seller.
def _bill() -> dict:
    return {
        "business": _Biz.slug,
        "payee": "0x" + "5e" * 20,
        "resource": "/saas/seats",
        "unit": "seat-month",
        "billed_usdc": 0.4,
        "billed_quantity": 50.0,
        "used_quantity": 12.0,
        "period_start": 900.0,
        "period_end": 1_100.0,
        "invoice_ref": "VENDOR-1001",
        "due_at": 2_000.0,
    }


def _wire_pass(monkeypatch, *, policy, businesses=None, bills=()):
    """A pass with a known tape, a known business and a known policy client.

    `bills` seeds the entitlement register. Empty by default, which is what
    every pre-existing test here expects and is also production's state today.
    """
    import index_api.marketplace as mkt

    monkeypatch.setattr(ok, "_businesses", lambda: businesses if businesses is not None else [_Biz()])
    monkeypatch.setattr(mkt, "build_receipts", lambda _fac: {"receipts": [_tape_row()]})
    monkeypatch.setattr(ok, "_policy_for", lambda _b: policy)
    import index_api.entitlements as ent

    monkeypatch.setattr(ent, "for_business", lambda _slug, path=None: list(bills))
    import index_api.statement as st

    monkeypatch.setattr(st, "read_decisions", lambda **_k: [])


class _Policy:
    """Enough of a PolicyClient to be paid through.

    `cash` is the wallet's BALANCE, which is not its budget. The default is
    deliberately generous so the cash rung never fires by accident in a test
    about something else; a test about liquidity passes a small one.
    """

    def __init__(self, cash: float | None = 1_000.0) -> None:
        self.spent: list[tuple] = []
        self._cash = cash

    def balance_usdc(self):
        return self._cash

    def signer_kinds(self):
        return {"agent": "circle", "owner": "local"}

    def budget(self, _category):
        return {"remaining_usdc": 100.0, "per_tx_limit_usdc": 50.0}

    def spend(self, category, to, amount, _record):
        self.spent.append((category, to, amount))
        return "0x" + "ee" * 32


def test_a_live_tick_actually_reaches_the_wallet(monkeypatch):
    """THE BUG THIS PINS. The first version of `_pass` called `run_obligation`
    with no `policy`, so `if d.intent == PAY and policy is not None` was never
    true: `live` mode recorded `intent: "pay"` with `paid_usdc: 0.0` and no
    `tx`. A decision labelled live that moved nothing — and because
    `settled_refs_from` ignores a row with no money on it, the same bill came
    back every tick for ever while the traction page counted the payment."""
    monkeypatch.setenv("ACR_OPERATOR_AUTORUN", "live")
    policy = _Policy()
    _wire_pass(monkeypatch, policy=policy)
    verdict = ok.tick_once(force=True)
    assert policy.spent, f"a live tick must reach the wallet; verdict was {verdict!r}"
    assert "live" in verdict and "USDC" in verdict


def test_a_dry_tick_never_reaches_the_wallet(monkeypatch):
    monkeypatch.setenv("ACR_OPERATOR_AUTORUN", "dry")
    policy = _Policy()
    _wire_pass(monkeypatch, policy=policy)
    ok.tick_once(force=True)
    assert policy.spent == [], "a dry run must not spend"


def test_the_cadence_reads_invoices_and_not_only_its_own_paywall(monkeypatch):
    """THE GAP THIS CLOSES. `obligations_from_entitlements` shipped with sixteen
    tests and no caller but `operator_run.py --entitlements <file>`, so on a
    schedule the agent could only see bills that settled through our own x402
    paywall. An invoice is where `due_at`, `invoice_ref` and an independently
    measured `used_quantity` come from — so check 7 (the timing) could never
    fire unattended, `settled_on_time` was 0 of 0 for that one reason, and the
    duplicate check's invoice-ref half was dead.

    Asserts the pairing, not just the count: `metered_quantity` is the seam that
    carries somebody else's usage export into check 3, and check 3 is skipped by
    a `None` guard that leaves no trace in the record. A bill arriving without
    its count would look identical from outside.
    """
    import index_api.operator as op

    seen: list[tuple[str, float | None, float | None]] = []
    real = op.run_obligation

    def spy(ob, **kw):
        seen.append((ob.resource, kw.get("metered_quantity"), ob.due_at))
        return real(ob, **kw)

    monkeypatch.setattr(op, "run_obligation", spy)
    monkeypatch.setenv("ACR_OPERATOR_AUTORUN", "dry")
    _wire_pass(monkeypatch, policy=_Policy(), bills=[_bill()])
    ok.tick_once(force=True)

    # The tape bill still runs, and the invoice runs beside it.
    assert any(r == "/compute/x" for r, _, _ in seen), seen
    invoice = [row for row in seen if row[0] == "/saas/seats"]
    assert invoice, f"the invoice never became an obligation: {seen}"
    resource, metered, due = invoice[0]
    assert metered == 12.0, "the independent count must ride with its bill"
    assert due == 2_000.0, "an invoice carries a due date; the tape never does"


def test_the_cadence_reads_the_balance_and_hands_one_picture_to_every_bill(monkeypatch):
    """The wiring between a chain read and a decision, which nothing else pins.

    `test_liquidity.py` proves the rung on an injected picture; this proves the
    picture is the one the WALLET answered, and that the pass reads it ONCE.
    One read per pass is the design: the balance does not change between two
    bills in the same tick, and a per-bill read would be N chain calls saying
    the same thing — and would let two decisions in one pass be judged against
    two different balances, which no reviewer could reconstruct afterwards.
    """
    import index_api.operator as op

    seen = []
    real = op.run_obligation

    def spy(ob, **kw):
        seen.append(kw.get("liquidity"))
        return real(ob, **kw)

    monkeypatch.setattr(op, "run_obligation", spy)
    monkeypatch.setenv("ACR_OPERATOR_AUTORUN", "dry")
    _wire_pass(monkeypatch, policy=_Policy(cash=4.25), bills=[_bill()])
    ok.tick_once(force=True)

    assert len(seen) >= 2, f"the pass decided fewer bills than it was given: {seen}"
    assert all(liq is seen[0] for liq in seen), "one picture per pass, not one per bill"
    assert seen[0] is not None and seen[0].measured is True
    assert seen[0].held_usdc == 4.25, "the figure must be the wallet's, not a default"


def test_a_wallet_that_will_not_answer_is_unmeasured_and_not_empty(monkeypatch):
    """And the pass still runs. A balance read that failed must not escalate
    every bill in the queue, which is what a confident 0.0 here would do."""
    import index_api.operator as op

    seen = []
    real = op.run_obligation

    def spy(ob, **kw):
        seen.append(kw.get("liquidity"))
        return real(ob, **kw)

    monkeypatch.setattr(op, "run_obligation", spy)
    monkeypatch.setenv("ACR_OPERATOR_AUTORUN", "dry")
    _wire_pass(monkeypatch, policy=_Policy(cash=None))
    ok.tick_once(force=True)

    assert seen, "the pass decided nothing at all"
    assert seen[0].measured is False
    assert seen[0].held_usdc is None, "unreadable is not zero"


def test_a_live_tick_with_no_wallet_refuses_rather_than_dry_running(monkeypatch):
    """"--live is refused for a business with no PolicyWallet rather than doing
    a dry run under the wrong name" is the runner's rule; the loop keeps it."""
    monkeypatch.setenv("ACR_OPERATOR_AUTORUN", "live")
    _wire_pass(monkeypatch, policy=None)
    verdict = ok.tick_once(force=True)
    assert "no_wallet" in verdict
    assert "pay" not in verdict, "and it must not report a payment"
