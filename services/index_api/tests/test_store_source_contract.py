"""`PrintStore` must honour what a `TapeSource` advertises about itself.

This file exists because of a shipped bug, and the bug is worth stating plainly:
`ChutesSource` advertised `native_step_s = 86_400` and
`requires_single_venue = True`, its module docstring said *"advertises that so a
caller cannot forget"* — and the only caller in the product, `app.py`'s bare
`PrintStore()`, forgot both. The result was not a crash. With
`ACR_TAPE_SOURCE=chutes` the index API produced **zero prints**: a daily tape cut
into hourly windows leaves 23 of every 24 empty, each empty window raised inside
`refresh`'s per-index `except`, and each exception was swallowed into a
`log.warning` that left the previous print in place. A feed that looks alive and
never moves — which `store.py`'s own docstring already names as the worst failure
mode a reference rate has.

Nothing in the suite caught it, because every test constructed `PrintStore` with
a simulator source whose defaults were already correct. So these tests pin the
contract from the other side: a source that declares something unusual, and the
assertion that the store adapts to it.

`test_stall_escalates` is the second half. Swallowing is still the right default
— one thin window must not take down the other two indices — but a *served*
index that cannot print for several cycles running is a broken feed, not a thin
window, and it has to say so at a level someone reads.

Hermetic: the contract tests use synthetic sources, so they cannot skip.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from acr_core import ALL_INDEX_IDS, SellerAttestation, Service, TapeEvent
from acr_sim import SimConfig
from acr_tape import ChutesSource, SimSource, TapeSource
from index_api.store import STALL_MISSES, PrintStore

FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "bt" / "chutes_usage.json"


class _Silent(TapeSource):
    """A source that yields nothing, declaring no policy of its own."""

    def stream(self) -> Iterator[TapeEvent]:
        return iter(())

    def attestations(self) -> list[SellerAttestation]:
        return []


class _Daily(_Silent):
    """A source declaring the same policy `ChutesSource` does."""

    native_step_s = 86_400.0
    requires_single_venue = True


class _InferenceOnly(_Silent):
    """A source that carries one service and says so."""

    covers_services = (Service.INFERENCE,)


# -- the windowing / single-venue contract ---------------------------------


def test_store_adopts_the_sources_windowing():
    st = PrintStore(source=_Daily())
    assert st.window_s == 86_400.0
    assert st.step_s == 86_400.0
    assert st.single_venue is True


def test_a_plain_source_keeps_the_old_defaults():
    """The regression guard: every existing caller must be unaffected."""
    st = PrintStore(source=_Silent())
    assert st.window_s == 3600.0
    assert st.step_s == 3600.0
    assert st.single_venue is False
    assert st.served == frozenset(ALL_INDEX_IDS)


def test_an_explicit_window_still_wins():
    """Reading the source is a DEFAULT, not a seizure of the caller's choice."""
    st = PrintStore(source=_Daily(), window_s=3600.0, step_s=1800.0)
    assert st.window_s == 3600.0
    assert st.step_s == 1800.0
    # the single-venue flag is not a windowing knob and is still taken from source
    assert st.single_venue is True


def test_sim_source_is_untouched():
    st = PrintStore(
        source=SimSource(config=SimConfig(seed=3, horizon=3600.0, events_per_service=200))
    )
    assert (st.window_s, st.step_s, st.single_venue) == (3600.0, 3600.0, False)


# -- service coverage -------------------------------------------------------


def test_declared_coverage_narrows_the_served_set():
    st = PrintStore(source=_InferenceOnly())
    assert st.served == frozenset({"ACR-INF"})


def test_unserved_indices_do_not_log_failures(caplog):
    """An index this source will NEVER carry is not a failure to report.

    It logged one every cycle, which is how the single real failure above stayed
    invisible: three warnings per refresh, two of them expected and permanent.
    """
    st = PrintStore(source=_InferenceOnly())
    with caplog.at_level(logging.WARNING, logger="index_api.store"):
        st.refresh()
    text = caplog.text
    assert "ACR-GPU" not in text
    assert "ACR-DATA" not in text


# -- the swallow ------------------------------------------------------------


def test_stall_escalates_after_repeated_misses(caplog):
    """One miss warns; `STALL_MISSES` in a row is an error.

    A served index that cannot print leaves the PREVIOUS print in place, so from
    the outside the feed is indistinguishable from a healthy one.
    """
    st = PrintStore(source=_Silent())
    with caplog.at_level(logging.WARNING, logger="index_api.store"):
        st.refresh()
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

        for _ in range(STALL_MISSES - 1):
            st.refresh()
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, f"no ERROR after {STALL_MISSES} consecutive misses"
        assert "not moving" in errors[-1].getMessage()


# -- the bug itself, end to end --------------------------------------------


def test_chutes_source_actually_prints():
    """The test whose absence let the break ship.

    Deliberately NOT `skipif(not FIXTURE.exists())`: a missing committed fixture
    is a broken checkout, and a test that skips looks exactly like a test that
    passes — the lesson `ci.yml` already learned for the on-chain suites.
    """
    assert FIXTURE.exists(), f"committed fixture missing: {FIXTURE}"

    st = PrintStore(source=ChutesSource())
    assert st.window_s == 86_400.0, "a daily tape must not be windowed hourly"
    assert st.single_venue is True
    st.refresh()

    assert "ACR-INF" in st.latest, "chutes produced no print at all"
    print_ = st.latest["ACR-INF"]
    assert print_.value > 0
    # A star-shaped funding graph cannot support an attack cost, and the
    # estimator is supposed to refuse rather than invent one.
    assert print_.attack_cost_per_bp is None
