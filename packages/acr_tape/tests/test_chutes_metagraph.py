"""Venue concentration — what the real-tape print carries instead of an attack cost.

`ChutesSource` refuses to publish `attack_cost_per_bp` on single-venue flow,
because the funding graph is a star and the cleaning stage has nothing to compare
against. That refusal is correct but it leaves the print saying nothing at all
about seller diversity, which is the thing an attack cost is ultimately a claim
about — and which, on Bittensor, happens to be directly observable.

These tests pin the measurement and, more importantly, pin the two places where
it must NOT overclaim: it is not an attack cost, and the IP field is not a
co-location signal however much it looks like one.

Runs off the committed fixture. No network, no API key.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from acr_tape import (
    CHUTES_NETUID,
    VenueConcentration,
    concentration,
    load_metagraph,
    venue_concentration,
)

FIXTURE = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "bt" / "chutes_metagraph.json"
)

pytestmark = pytest.mark.skipif(
    not FIXTURE.exists(),
    reason="no metagraph fixture (run `uv run python scripts/bt_snapshot.py --capture-fixture`)",
)


@pytest.fixture(scope="module")
def vc() -> VenueConcentration:
    out = venue_concentration()
    assert out is not None
    return out


def test_it_measures_the_real_subnet(vc):
    assert vc.netuid == CHUTES_NETUID
    assert vc.n_hotkeys > 0
    # Fewer coldkeys than hotkeys is the whole point: some actors wear several
    # faces. If these were ever equal the diagnostic would be measuring nothing.
    assert vc.n_coldkeys <= vc.n_hotkeys


def test_multi_hotkey_actors_exist_and_are_counted(vc):
    """Measured 2026-10-29: 26 coldkeys controlling more than one hotkey, max 8."""
    assert vc.multi_hotkey_coldkeys > 0
    assert vc.largest_cluster > 1
    assert sum(vc.hotkeys_per_coldkey.values()) == vc.n_coldkeys


def test_sybil_headroom_is_the_difference_not_a_cost(vc):
    """`n_hotkeys - n_coldkeys` — extra faces, denominated in identities.

    Named headroom rather than cost on purpose. It says how many hotkeys are a
    second face rather than a second seller; it says nothing about dollars.
    """
    assert vc.sybil_headroom == vc.n_hotkeys - vc.n_coldkeys
    assert vc.sybil_headroom > 0


def test_ip_is_reported_as_unusable(vc):
    """The negative result, pinned so nobody later builds on the IP field.

    256 of 256 rows carry an `ip`, which looks like a usable co-location signal
    until you count distinct values: 8 real ones, and 248 rows holding the
    literal string "0". That is a redaction, not co-location.
    """
    assert vc.ip_usable is False
    assert vc.distinct_ips < vc.n_hotkeys


def test_identity_hhi_is_bounded_and_above_the_uniform_floor(vc):
    """HHI's floor is 1/n when every actor is equal; concentration pushes it up."""
    assert 0.0 < vc.identity_hhi <= 1.0
    assert vc.identity_hhi >= 1.0 / vc.n_coldkeys


def test_stake_is_far_more_concentrated_than_identity(vc):
    """Two different stories, which is why both are reported.

    Measured: 207 distinct coldkeys, but only 58 of 256 hotkeys carry any stake
    and one coldkey holds the clear majority of it. Many sellers, very little of
    the weight — an identity count alone would read as healthier than the venue
    is.
    """
    assert vc.staked_hotkeys < vc.n_hotkeys
    assert vc.stake_hhi > vc.identity_hhi


def test_summary_names_the_venue_and_omits_any_cost(vc):
    s = vc.summary()
    assert str(CHUTES_NETUID) in s
    assert "hotkeys" in s and "coldkeys" in s
    # The wording guard: this must never read as a price or a bound.
    for forbidden in ("usd", "$", "cost", "attack"):
        assert forbidden not in s.lower()


# --- shape and degradation -------------------------------------------------


def test_rows_for_another_netuid_are_dropped_not_merged():
    """Concentration is a property of ONE venue; blending two describes neither."""
    rows = [
        {"netuid": 64, "hotkey": "h1", "coldkey": "c1", "stake": 1.0, "ip": "1.2.3.4"},
        {"netuid": 51, "hotkey": "h2", "coldkey": "c2", "stake": 1.0, "ip": "1.2.3.5"},
    ]
    assert concentration(rows, netuid=64).n_hotkeys == 1
    assert concentration(rows, netuid=51).n_hotkeys == 1


def test_netuid_zero_is_measured_not_dropped():
    """Regression: `int(row.get("netuid") or -1)` silently discarded the root network.

    `0` is falsy, so every netuid-0 row collapsed to -1 and was filtered out, and
    `concentration(rows, netuid=0)` returned a venue with no sellers however many
    valid rows it was handed — a wrong measurement rather than a refusal, which is
    the worst of the available failures. Unreachable from shipped code today
    (`CHUTES_NETUID` is 64 and the Taostats allowlist is 51/64), so this test is
    the only thing standing between the next caller and that trap.
    """
    rows = [
        {"netuid": 0, "hotkey": "h1", "coldkey": "c1", "stake": 1.0, "ip": "0"},
        {"netuid": 0, "hotkey": "h2", "coldkey": "c1", "stake": 2.0, "ip": "0"},
        {"netuid": 64, "hotkey": "h3", "coldkey": "c9", "stake": 1.0, "ip": "0"},
    ]
    out = concentration(rows, netuid=0)
    assert out.n_hotkeys == 2, "netuid 0 rows were dropped"
    assert out.n_coldkeys == 1
    assert out.largest_cluster == 2
    assert out.netuid == 0
    # And the filter still excludes the other subnet.
    assert concentration(rows, netuid=64).n_hotkeys == 1


def test_a_row_with_no_netuid_or_a_junk_one_is_excluded():
    """Missing and unparseable both mean 'does not state a venue', which is not
    the same as stating netuid 0 — the distinction the fix exists to keep."""
    rows = [
        {"hotkey": "h1", "coldkey": "c1", "stake": 1.0},
        {"netuid": "not-a-number", "hotkey": "h2", "coldkey": "c2", "stake": 1.0},
        {"netuid": 0, "hotkey": "h3", "coldkey": "c3", "stake": 1.0},
    ]
    assert concentration(rows, netuid=0).n_hotkeys == 1


def test_an_empty_metagraph_is_zeroed_not_a_crash():
    out = concentration([], netuid=64)
    assert out.n_hotkeys == 0
    assert out.identity_hhi == 0.0
    assert out.sybil_headroom == 0


def test_a_missing_fixture_yields_none_rather_than_raising(tmp_path):
    assert load_metagraph(tmp_path / "absent.json") == []
    assert venue_concentration(tmp_path / "absent.json") is None


def test_one_coldkey_holding_everything_is_hhi_one():
    rows = [
        {"netuid": 64, "hotkey": f"h{i}", "coldkey": "solo", "stake": 5.0, "ip": "0"}
        for i in range(6)
    ]
    out = concentration(rows, netuid=64)
    assert out.n_coldkeys == 1
    assert out.identity_hhi == pytest.approx(1.0)
    assert out.largest_cluster == 6
    assert out.sybil_headroom == 5


def test_ip_becomes_usable_when_the_addresses_are_real():
    """The detector keys on evidence, not on the field being populated."""
    rows = [
        {"netuid": 64, "hotkey": f"h{i}", "coldkey": f"c{i}", "stake": 1.0,
         "ip": f"10.0.0.{i}"}
        for i in range(8)
    ]
    assert concentration(rows, netuid=64).ip_usable is True
