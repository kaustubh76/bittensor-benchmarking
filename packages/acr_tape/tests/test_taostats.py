"""The Taostats adapter — optional, budgeted, and offline by default.

Three things these tests exist to hold, each from something measured against the
live API rather than imagined:

1. **No key must mean no network and no exception.** The base install has to stay
   deployable with no credentials at all, so every method returns ``None`` and
   the subnet keeps printing. A client that raised here would take the whole
   fixing cycle down over an optional enrichment.
2. **The credit budget must actually bind.** The free tier is 5 requests/minute
   and 10,000 a month. During development a capture run hit the per-minute limit
   mid-way and silently lost netuid 64's coldkey distribution — so the 429 path
   and the per-cycle cap are both pinned.
3. **RAO is not TAO.** `registration_cost` arrives as a string in RAO. The
   difference between 259043206400 and 259.04 is the difference between a
   nonsense figure in a submission and a true one.

Everything runs off the recorded fixture. No key, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from acr_tape import (
    MAX_CALLS_PER_CYCLE,
    RAO,
    ROUTES,
    RegistrationCost,
    SubnetPool,
    TaostatsBudgetExhausted,
    TaostatsClient,
    load_fixture,
    pool_from_fixture,
    registration_cost_from_fixture,
)

FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "bt" / "taostats.json"


# --- offline by default ---------------------------------------------------


def test_no_key_means_offline_and_silent():
    c = TaostatsClient(api_key="")
    assert c.offline is True
    assert c.tao_price_usd() is None
    assert c.registration_cost() is None
    assert c.subnet_pool(64) is None
    assert c.coldkey_distribution(64) is None
    # And it never spent a call trying.
    assert c.calls == 0


def test_a_commented_out_key_is_not_a_key():
    """A .env line like `#ACR_TAOSTATS_API_KEY=...` must read as absent.

    The same guard `acr_oracle_client.signer.build_signer` applies, and for the
    same reason: a commented placeholder that looks configured sends the client
    at the network with a bad credential.
    """
    assert TaostatsClient(api_key="#tao-not-a-real-key").offline is True


def test_a_netuid_outside_the_allowlist_is_refused_without_a_call():
    """A sweep over 128 subnets would spend a month of credits in one cycle."""
    c = TaostatsClient(api_key="k", netuids=(51, 64))
    assert c.subnet_pool(1) is None
    assert c.coldkey_distribution(1) is None
    assert c.calls == 0


# --- the budget -----------------------------------------------------------


def test_the_per_cycle_budget_binds_and_says_so():
    c = TaostatsClient(api_key="k", max_calls=2)
    c.calls = 2
    with pytest.raises(TaostatsBudgetExhausted, match="max 2"):
        c._get("price", {"asset": "tao"})


def test_reset_cycle_restores_the_budget_and_clears_the_cache():
    c = TaostatsClient(api_key="k", max_calls=1)
    c.calls = 1
    c.rate_limited = True
    c._cache["x"] = 1
    c.reset_cycle()
    assert c.calls == 0
    assert c.rate_limited is False
    assert c._cache == {}


def test_a_rate_limited_cycle_stops_asking():
    """A retry storm against a 5/minute cap loses the month's budget."""
    c = TaostatsClient(api_key="k")
    c.rate_limited = True
    before = c.calls
    assert c._get("price", {"asset": "tao"}) is None
    assert c.calls == before, "a rate-limited client must not spend another call"


def test_the_default_budget_fits_the_free_tier():
    """10,000/month over hourly cycles is ~13.9 an hour; the cap is 5 a minute."""
    assert MAX_CALLS_PER_CYCLE <= 5
    assert MAX_CALLS_PER_CYCLE * 24 * 31 < 10_000


# --- the route table ------------------------------------------------------


def test_the_route_table_is_data_in_one_place():
    """The `/api/...` routes retire 2026-11-09, so the cutover is one dict edit."""
    assert set(ROUTES) == {"price", "coldkeys", "registration", "emission"}
    # /v1/price is already on the post-retirement scheme.
    assert ROUTES["price"].startswith("/v1/")


#: The announced retirement of the `/api/...` scheme. Three of the four routes
#: above are on it and the replacements are not published, so there is nothing
#: to migrate TO yet — which is exactly why this needs a date rather than a
#: mental note.
API_SCHEME_RETIRES = "2026-11-09"


def test_the_api_scheme_retirement_is_tracked_by_a_clock_not_a_comment():
    """Goes RED the day the old scheme retires, while routes still use it.

    The retirement was documented in five places (`.env.example`,
    `docs/BITTENSOR.md`, `taostats_client.ROUTES`, `acr_core.config`, and the
    test above) and watched by none of them. Chain state is OPTIONAL and no-ops
    without a key — `ACR_TAOSTATS_API_KEY` is absent from `render.yaml`
    entirely — so the failure would not break the product. It would make every
    chain-state read quietly return nothing, for as long as it took somebody to
    notice, and a comment carrying a date cannot notice anything.

    This test is the noticing. It is not a prediction that the routes will
    break; it is a calendar entry that fails loudly, with the route list in the
    message, on the one day a human should look.
    """
    from datetime import UTC, datetime

    legacy = sorted(k for k, v in ROUTES.items() if v.startswith("/api/"))
    if not legacy:
        # Already migrated. Nothing to warn about, and the date may go.
        return
    today = datetime.now(UTC).date().isoformat()
    assert today < API_SCHEME_RETIRES, (
        f"Taostats retired the /api/... scheme on {API_SCHEME_RETIRES} and "
        f"{len(legacy)} route(s) still use it ({', '.join(legacy)}). Chain state "
        f"is optional, so these fail SILENTLY: `_get` returns None and every "
        f"caller treats that as 'no data'. Re-probe the endpoints, update "
        f"`taostats_client.ROUTES`, and move this date or delete it."
    )


def test_the_broken_pool_route_is_absent_on_purpose():
    """`/api/dtao/pool/latest/v1` returns HTTP 520 — persistently, server-side.

    Alpha price is derived from the emission route instead, which carries both
    reserves. A route that does not work has no business in the table.
    """
    assert not any("pool/latest" in p for p in ROUTES.values())
    assert "emission" in ROUTES


def test_an_unknown_route_key_is_none_not_a_crash():
    c = TaostatsClient(api_key="k")
    assert c._get("no-such-route") is None


# --- envelope and units ---------------------------------------------------


def test_the_pagination_envelope_is_unwrapped():
    payload = {"pagination": {"total_items": 1}, "data": [{"price": "1.5"}]}
    assert TaostatsClient._rows(payload) == [{"price": "1.5"}]


def test_a_bare_list_also_unwraps():
    assert TaostatsClient._rows([{"a": 1}]) == [{"a": 1}]


@pytest.mark.parametrize("junk", [None, {}, {"data": "not a list"}, 7, "x"])
def test_a_malformed_payload_is_no_rows(junk):
    assert TaostatsClient._rows(junk) == []


def test_registration_cost_converts_rao_to_tao():
    rc = RegistrationCost(rao=259_043_206_400, block_number=9_388_680, timestamp="t")
    assert rc.tao == pytest.approx(259.0432064)
    assert RAO == 1_000_000_000


def test_alpha_price_is_the_reserve_ratio():
    """Taostats' own definition: TAO pool reserve over alpha pool reserve."""
    p = SubnetPool(
        netuid=64,
        block_number=1,
        timestamp="t",
        tao_in_pool=8_782_111.0,
        alpha_in_pool=130_471_567.0,
    )
    assert p.alpha_price_tao == pytest.approx(8_782_111.0 / 130_471_567.0)
    assert p.alpha_price_tao == pytest.approx(0.0673, abs=1e-4)


def test_an_empty_alpha_side_has_no_price_rather_than_a_free_one():
    p = SubnetPool(netuid=64, block_number=1, timestamp="t", tao_in_pool=10.0, alpha_in_pool=0.0)
    assert p.alpha_price_tao is None


# --- the recorded fixture -------------------------------------------------


@pytest.mark.skipif(
    not FIXTURE.exists(),
    reason="no taostats fixture (capture needs ACR_TAOSTATS_API_KEY)",
)
class TestRecordedFixture:
    def test_it_loads(self):
        assert load_fixture() is not None

    def test_it_carries_a_plausible_tao_price(self):
        fx = load_fixture()
        px = fx.get("tao_price_usd")
        assert px is not None
        # A wide band: this tracks a live market. Tight enough to catch a unit
        # error or a parse that picked up market cap instead of price.
        assert 1.0 < float(px) < 100_000.0

    def test_the_registration_cost_reads_as_hundreds_of_tao_not_billions(self):
        """The RAO trap, pinned. Round 1's docs quoted ~349 TAO from an
        unofficial dashboard; the authoritative live figure is ~259."""
        rc = registration_cost_from_fixture()
        assert rc is not None
        assert rc.rao > 1_000_000_000  # it really is in RAO
        assert 1.0 < rc.tao < 100_000.0  # and converts to a sane TAO figure
        assert rc.block_number > 0

    def test_chutes_pool_implies_an_alpha_price(self):
        p = pool_from_fixture(64)
        assert p is not None
        assert p.tao_in_pool > 0 and p.alpha_in_pool > 0
        assert p.alpha_price_tao is not None and p.alpha_price_tao > 0

    def test_a_netuid_absent_from_the_fixture_is_none(self):
        assert pool_from_fixture(999) is None

    def test_the_coldkey_count_agrees_with_the_chutes_metagraph(self):
        """The strongest corroboration in this integration.

        Taostats' `distribution/coldkey` and Chutes' `miner/metagraph` are
        unrelated sources describing the same subnet, and they agree on the
        number of distinct coldkeys. Worth a test precisely because it is
        redundant: a disagreement means one of them is wrong, and we would want
        to find out from a red suite rather than from a reader.

        Skipped when netuid 64's distribution is not in the fixture — the
        per-minute rate limit cost us that call during capture, which is itself
        the reason the budget guard exists.
        """
        from acr_tape import venue_concentration

        fx = load_fixture() or {}
        rec = (fx.get("coldkeys") or {}).get("64")
        if not rec:
            pytest.skip("netuid 64 coldkey distribution not captured (rate limit)")
        vc = venue_concentration()
        assert vc is not None
        assert rec["n_coldkeys"] == vc.n_coldkeys
        assert rec["n_hotkeys"] == vc.n_hotkeys


def test_the_fixture_contains_no_credential():
    """A recorded response must never carry the key that fetched it."""
    if not FIXTURE.exists():
        pytest.skip("no fixture")
    raw = json.dumps(json.loads(FIXTURE.read_text()))
    for marker in ("tao-", "Authorization", "api_key", "apiKey"):
        assert marker not in raw
