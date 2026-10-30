"""Attack Lab run hygiene: the live run honors the sim-size knob, and the run
slot is single-flight even across a watchdog false-error (no concurrent sims —
two full sims interleaving into one series was also an OOM on a 512MB box)."""

from __future__ import annotations

import time

from index_api import demo


class _Settings:
    attack_sim_events_per_service = 400


def _reset():
    with demo._lock:
        demo._busy = False
    demo._run = demo.AttackRun()


def test_events_per_service_mirrors_exhibit_scaling():
    # attack.py uses 4/5 of the per-hour budget; 12 hours total.
    assert demo.events_per_service(_Settings()) == 12 * (400 * 4 // 5)

    class Default:
        attack_sim_events_per_service = 2_500

    # Default preserves the historical rich run (24 000 events/service).
    assert demo.events_per_service(Default()) == 24_000


def test_second_start_refused_while_running():
    _reset()
    assert demo.try_start(8000.0, 2.5, 1)
    assert not demo.try_start(8000.0, 2.5, 2)
    _reset()


def test_watchdog_error_does_not_release_the_slot():
    _reset()
    assert demo.try_start(8000.0, 2.5, 1)
    # Simulate a stalled worker: age the progress stamp past the watchdog.
    with demo._lock:
        demo._run.stepped_at = time.monotonic() - demo.STEP_TIMEOUT_S - 1
    st = demo.status()
    assert st["state"] == "error"  # displayed as stalled…
    assert not demo.try_start(8000.0, 2.5, 2)  # …but the slot stays claimed
    _reset()


def test_execute_releases_the_slot_even_on_failure(monkeypatch):
    _reset()
    assert demo.try_start(8000.0, 2.5, 1)

    def boom(*a):
        raise RuntimeError("sim exploded")

    monkeypatch.setattr(demo, "_execute", boom)
    demo.execute(8000.0, 2.5, 1)
    assert demo.status()["state"] == "error"
    # Slot released by the finally — a fresh run can start.
    assert demo.try_start(8000.0, 2.5, 3)
    _reset()
