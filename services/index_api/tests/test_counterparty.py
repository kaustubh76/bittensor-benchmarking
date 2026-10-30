"""Counterparty screening — and the one line that matters most.

`test_a_screen_that_cannot_answer_is_never_clear`. A screening service that
times out must not read as a clean bill of health. Everything else in this file
is ordinary; that one is the reason the module has three risk states instead of
a boolean.
"""

from __future__ import annotations

from index_api.counterparty import (
    CLEAR,
    FLAGGED,
    UNKNOWN,
    CounterpartyScreen,
    CounterpartyVerdict,
    DenyListScreen,
    NullCounterpartyScreen,
    YenteScreen,
    build_screen,
    get_screen,
    reset_screen,
    set_screen,
)

A = "0x" + "aa" * 20
BAD = "0x" + "bb" * 20


# --- the fail-safe direction ----------------------------------------------

def test_a_screen_that_cannot_answer_is_never_clear():
    """The single most important behaviour here. A third party's outage must not
    become a clean result."""

    class Broken(CounterpartyScreen):
        backend = "broken"

        def _check(self, address):
            raise RuntimeError("connection refused")

    v = Broken().check(A)
    assert v.risk == UNKNOWN
    assert v.risk != CLEAR
    assert v.screened is False, "and it says no screen ran"
    assert "could not answer" in v.reason


def test_check_never_raises_so_an_outage_is_not_a_bug():
    """Raising would make a third party being down indistinguishable from a
    defect in the operator, and the response to either is the same."""

    class Broken(CounterpartyScreen):
        backend = "broken"

        def _check(self, address):
            raise TimeoutError("too slow")

    Broken().check(A)  # must not raise


def test_an_empty_address_is_unknown_not_clear():
    assert DenyListScreen().check("").risk == UNKNOWN
    assert DenyListScreen().check("   ").screened is False


# --- what "payable" means -------------------------------------------------

def test_a_flagged_counterparty_is_never_payable_by_the_agent():
    v = CounterpartyVerdict(address=BAD, risk=FLAGGED, backend="x")
    assert v.payable is False


def test_a_clear_counterparty_is_payable():
    assert CounterpartyVerdict(address=A, risk=CLEAR, backend="x").payable is True


def test_unknown_is_payable_by_default_and_not_when_screening_is_required(monkeypatch):
    """An operator that refuses everything without a screening service is an
    operator nobody runs. The gap is recorded either way; only the consequence
    is configurable."""
    from index_api import counterparty as cp

    v = CounterpartyVerdict(address=A, risk=UNKNOWN, backend="off")
    monkeypatch.setattr(cp, "REQUIRED", False)
    assert v.payable is True
    monkeypatch.setattr(cp, "REQUIRED", True)
    assert v.payable is False


def test_screened_is_a_separate_fact_from_clear():
    """"No screen ran" and "ran and found nothing" must not render the same.

    This test used to assert that an EMPTY `DenyListScreen` answers `clear`,
    which is the very collapse the docstring forbids: a list of zero addresses
    cannot find anything, so "ran and found nothing" and "nothing ran" were the
    same event wearing different words. The honest comparison needs a list with
    something in it.
    """
    off = NullCounterpartyScreen().check(A)
    assert off.risk == UNKNOWN and off.screened is False

    nothing_to_compare = DenyListScreen().check(A)
    assert nothing_to_compare.risk == UNKNOWN, "an empty list is not a screen"
    assert nothing_to_compare.screened is False

    real = DenyListScreen(denied={"0x" + "ff" * 20}).check(A)
    assert real.risk == CLEAR and real.screened is True


def test_a_screen_with_nothing_to_compare_says_so():
    """The deployed default today: no ACR_SCREEN_* is set on either service.

    Every vendor rendered a teal "clear · checked, fine" chip whose whole basis
    was a comparison against zero addresses — and `backend`/`reason` are dropped
    at the decision boundary, so no reader could ever have found that out.
    """
    v = DenyListScreen().check(A)
    assert v.risk == UNKNOWN
    assert "no denylist is configured" in v.reason
    assert v.backend == "denylist", "it still says which floor answered"


def test_a_configured_denylist_still_flags_and_still_clears():
    """The fix must not disarm the floor it is making honest."""
    bad = "0x" + "ff" * 20
    screen = DenyListScreen(denied={bad})
    assert screen.check(bad).risk == FLAGGED
    assert screen.check(A).risk == CLEAR


# --- the local floor ------------------------------------------------------

def test_the_denylist_flags_what_is_on_it_and_names_the_list():
    s = DenyListScreen(denied={BAD})
    bad = s.check(BAD)
    assert bad.risk == FLAGGED
    assert bad.matched == ("local-denylist",)
    assert s.check(A).risk == CLEAR


def test_the_denylist_ignores_address_case():
    s = DenyListScreen(denied={BAD.lower()})
    assert s.check(BAD.upper()).risk == FLAGGED


def test_the_denylist_reads_the_environment(monkeypatch):
    monkeypatch.setenv("ACR_SCREEN_DENYLIST", f"{BAD}, {A}")
    s = DenyListScreen()
    assert s.check(BAD).risk == FLAGGED
    assert s.check(A).risk == FLAGGED


def test_the_floor_says_how_small_it_is():
    """It answers `clear` only for addresses it really compared, and the reason
    says against how many. A floor that reads like coverage is the danger."""
    assert "not on a local list of 1" in DenyListScreen(denied={BAD}).check(A).reason


# --- counters -------------------------------------------------------------

def test_a_screen_counts_what_it_checked_and_what_it_flagged():
    s = DenyListScreen(denied={BAD})
    s.check(A)
    s.check(BAD)
    assert (s.checked, s.flagged) == (2, 1)


def test_a_failed_check_is_not_counted_as_checked():
    class Broken(CounterpartyScreen):
        backend = "broken"

        def _check(self, address):
            raise RuntimeError("nope")

    s = Broken()
    s.check(A)
    assert s.checked == 0, "nothing was actually screened"


# --- yente ----------------------------------------------------------------

def test_yente_reports_a_hit_as_flagged_and_names_the_datasets(monkeypatch):
    """A hit is reported, not refused: `decide()` chooses, because "medium risk
    gets a lower limit, not a refusal" is the RFB's framing and a screen that
    decides policy cannot be reused by a business with a different one."""

    class _R:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"dataset": "us_ofac_sdn"}, {"dataset": "eu_fsf"}]}

    monkeypatch.setattr("httpx.get", lambda *a, **k: _R())
    v = YenteScreen("http://yente:8000").check(BAD)
    assert v.risk == FLAGGED
    assert v.matched == ("eu_fsf", "us_ofac_sdn")
    assert "2 match" in v.reason


def test_yente_reports_no_hit_as_clear(monkeypatch):
    class _R:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

    monkeypatch.setattr("httpx.get", lambda *a, **k: _R())
    v = YenteScreen("http://yente:8000").check(A)
    assert v.risk == CLEAR and v.screened is True


def test_a_yente_error_is_unknown_never_clear(monkeypatch):
    def boom(*a, **k):
        raise OSError("dns failure")

    monkeypatch.setattr("httpx.get", boom)
    assert YenteScreen("http://yente:8000").check(A).risk == UNKNOWN


# --- configuration --------------------------------------------------------

def test_yente_is_chosen_when_a_url_is_configured(monkeypatch):
    monkeypatch.setenv("ACR_SCREEN_YENTE_URL", "http://yente:8000")
    assert isinstance(build_screen(), YenteScreen)


def test_off_is_a_configuration_not_an_absence(monkeypatch):
    monkeypatch.delenv("ACR_SCREEN_YENTE_URL", raising=False)
    monkeypatch.setenv("ACR_SCREEN_MODE", "off")
    s = build_screen()
    assert isinstance(s, NullCounterpartyScreen)
    assert s.check(A).reason == "counterparty screening is switched off"


def test_the_default_is_the_local_floor(monkeypatch):
    monkeypatch.delenv("ACR_SCREEN_YENTE_URL", raising=False)
    monkeypatch.delenv("ACR_SCREEN_MODE", raising=False)
    assert isinstance(build_screen(), DenyListScreen)


def test_the_registry_is_a_test_seam():
    reset_screen()
    try:
        mine = DenyListScreen(denied={BAD})
        set_screen(mine)
        assert get_screen() is mine
    finally:
        reset_screen()


def test_the_verdict_serialises_for_the_record():
    v = DenyListScreen(denied={BAD}).check(BAD)
    d = v.as_dict()
    assert d["risk"] == FLAGGED
    assert d["matched"] == ["local-denylist"]
    assert d["address"] == BAD and d["screened"] is True
