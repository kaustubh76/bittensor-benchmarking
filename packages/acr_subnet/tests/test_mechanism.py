"""The mechanism end to end: does the subnet pay the right miner?

This is the only question the subnet exists to answer, and it is not answerable
by inspection — each assertion below failed at some point during construction
while the code ran perfectly and simply rewarded the wrong strategy.

Runs in-process: no chain, no netuid, no network, no `bittensor` dependency.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from acr_core import ACRPrint
from acr_subnet import (
    CopycatMiner,
    Epoch,
    FourPillarMiner,
    OverclaimMiner,
    Validator,
    VwapMiner,
    real_tape_epochs,
    run,
    synthetic_epochs,
)

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "redteam"))

from optimal_attack import run_optimal_attack  # noqa: E402

SEED = 4242  # never 21: gen_evalset.py records that as the favourable published seed
FIXTURE = _ROOT / "tests" / "fixtures" / "bt" / "chutes_usage.json"


def miners():
    return [
        FourPillarMiner(uid=1),
        VwapMiner(uid=2, width_bp=500.0),
        CopycatMiner(uid=3),
        OverclaimMiner(uid=4, multiple=50.0),
    ]


@pytest.fixture(scope="module")
def synth_report():
    eps = synthetic_epochs("ACR-INF", 6, seed=SEED, events_per_service=2000)
    assert eps, "the simulator produced no epochs"
    return run("ACR-INF", miners(), eps, validator=Validator(attack_fn=run_optimal_attack))


# --- the orderings that are the whole point -------------------------------


def test_four_pillar_outranks_naive_vwap(synth_report):
    fp = synth_report.by_strategy("four-pillar")
    vw = synth_report.by_strategy("naive-vwap")
    assert fp.total_weight > vw.total_weight


def test_naive_vwap_is_both_less_accurate_and_less_resistant(synth_report):
    """Resistance must discriminate between estimators, not just exist.

    It did not at first: resistance was measured once per epoch against the
    tape, so every miner received an identical 0.9175 and the component was
    decorative. Moving a volume-weighted average is the easiest thing an
    attacker can do, so a mechanism where a VWAP scores the same as the
    four-pillar estimator on resistance is measuring the wrong thing.
    """
    fp = synth_report.by_strategy("four-pillar")
    vw = synth_report.by_strategy("naive-vwap")
    assert fp.mean_accuracy > vw.mean_accuracy
    assert fp.mean_resistance is not None and vw.mean_resistance is not None
    assert fp.mean_resistance > vw.mean_resistance


def test_the_over_claiming_miner_is_penalised_despite_an_identical_estimate(synth_report):
    """Same value, same interval, same resistance — only the claim differs."""
    fp = synth_report.by_strategy("four-pillar")
    oc = synth_report.by_strategy("overclaim")
    assert fp.mean_accuracy == pytest.approx(oc.mean_accuracy)
    assert oc.mean_attainability < fp.mean_attainability
    assert oc.total_weight < fp.total_weight


def test_the_copycat_earns_nothing(synth_report):
    cc = synth_report.by_strategy("copycat")
    assert cc.total_weight == pytest.approx(0.0)


def test_the_copycat_would_otherwise_have_won(synth_report):
    """The honest reason copying is refused, recorded so the claim is not smug.

    A lagged copy earns near-full accuracy (a stale reference rate really is
    almost as good on a slow-moving level) and PERFECT resistance (a print that
    never reads the tape cannot be moved by poisoning the tape). Both are true.
    Together they would pay a free-rider more than the only participant doing
    the work, which is why the copy is detected and zeroed rather than left to
    lose on merit — it would not lose.
    """
    cc = synth_report.by_strategy("copycat")
    assert cc.mean_accuracy is not None and cc.mean_accuracy > 0.5
    assert cc.mean_resistance == pytest.approx(1.0)
    assert cc.total_weight == pytest.approx(0.0)  # and still paid nothing


def test_every_epoch_published_a_consensus_print(synth_report):
    assert len(synth_report.consensus_prints) == len(synth_report.results)
    for c in synth_report.consensus_prints:
        assert isinstance(c, ACRPrint)
        assert c.ci_lo <= c.value <= c.ci_hi
        assert c.attack_cost_per_bp is not None  # a real graph -> a real bound


def test_consensus_tracks_the_latent_rather_than_sitting_still(synth_report):
    """Epochs are consecutive windows of ONE path, so the level must drift.

    Independent per-epoch simulations all start from the same reference level,
    the published rate then barely moves, and a copier gets a free ride — which
    is exactly how the copycat beat every real estimator before the harness was
    fixed.
    """
    vals = [c.value for c in synth_report.consensus_prints]
    assert len(set(vals)) > 1, "the consensus never moved; epochs are not a time series"


# --- abstention -----------------------------------------------------------


def test_a_miner_may_abstain_without_being_scored():
    """Abstaining is correct behaviour on a window a miner cannot handle, and
    must not be punished below submitting a fabricated print."""
    m = FourPillarMiner(uid=1)
    assert m.submit("ACR-INF", 0, [], [], ts=1.0) is None


def test_an_epoch_with_no_submissions_publishes_nothing():
    v = Validator()
    res = v.run_epoch(Epoch(index_id="ACR-INF", number=0, events=[], attestations=[]), miners())
    assert res.consensus is None
    assert "no miner submitted" in res.note


# --- the real Bittensor tape ----------------------------------------------


@pytest.fixture(scope="module")
def real_report():
    from acr_tape import ChutesSource

    v = Validator(attack_fn=run_optimal_attack)
    ms = miners()
    # Scoring epochs first, to earn weights. Real single-venue tape cannot score
    # anyone, so it publishes under weights earned on known truth -- the order a
    # real benchmark is run in.
    warm = synthetic_epochs("ACR-INF", 5, seed=SEED, events_per_service=2000)
    run("ACR-INF", ms, warm, validator=v)
    eps = real_tape_epochs("ACR-INF", ChutesSource())
    assert eps, "no real-tape epochs from the fixture"
    return run("ACR-INF", ms, eps, validator=v)


@pytest.mark.skipif(
    not FIXTURE.exists(),
    reason="no Bittensor fixture (run `uv run python scripts/bt_snapshot.py --capture-fixture`)",
)
class TestRealTape:
    def test_it_publishes_prints_from_real_bittensor_flow(self, real_report):
        assert real_report.consensus_prints

    def test_it_refuses_to_publish_an_attack_cost(self, real_report):
        """A price survives single-venue flow; an attack cost does not.

        The funding graph is a star — one aggregate buyer paying many sellers —
        so there is no counterparty diversity for an adversary to have to
        manufacture, and a bound computed anyway would be the cluster cap's
        artefact dressed up as a defence.
        """
        for c in real_report.consensus_prints:
            assert c.attack_cost_per_bp is None

    def test_accuracy_is_not_scored_on_real_tape(self, real_report):
        """There is no latent behind a live market and no honest way to make one."""
        for res in real_report.results:
            assert res.epoch.truth is None
            assert res.scored is False
            for sc in res.scores:
                assert sc.accuracy is None

    def test_weights_carry_forward_from_the_last_scored_epoch(self, real_report):
        """One gated epoch used to erase the carry-forward and silence a whole
        twelve-epoch run, because an empty weight dict overwrote the good one."""
        fp = real_report.by_strategy("four-pillar")
        vw = real_report.by_strategy("naive-vwap")
        assert fp.total_weight > 0.0
        assert fp.total_weight > vw.total_weight

    def test_the_epochs_declare_themselves_single_venue(self, real_report):
        for res in real_report.results:
            assert res.epoch.single_venue is True


# --- the adversary family: resistance has to be measured with more than one ----


@pytest.fixture(scope="module")
def one_epoch():
    """A single wide window — the shape the on-chain runs used.

    Not the 6-window `synth_report` fixture, deliberately. The single-adversary
    mechanism ranked correctly on that fixture and *inverted* on a single wide
    window, so the regression this file needs is the wide one.
    """
    return synthetic_epochs("ACR-INF", 1, seed=SEED)[0]


def test_an_out_of_band_wash_hurts_a_vwap_far_more_than_an_in_band_one(one_epoch):
    """Why one adversary was never enough.

    `build_injection` prices its wash off `m0`, the trimmed weighted median —
    the four-pillar estimator's own statistic. So an in-band member attacks that
    estimator and leaves a volume-weighted mean almost untouched, which is how a
    naive VWAP came to measure as six times more resistant while simply not
    being the one under attack. Priced outside the trim band the position
    reverses: the trimmed median discards the injection entirely and the mean,
    which trims nothing, takes the full hit.
    """
    v = Validator(attack_fn=run_optimal_attack)
    family = {name: (inj, fees) for name, inj, fees in v.build_attack_family(one_epoch)}
    assert set(family) == {"in-band", "out-of-band"}, "both members must build"

    moved = {}
    for miner in (FourPillarMiner(uid=0), VwapMiner(uid=1)):
        clean = miner.submit(
            one_epoch.index_id, one_epoch.number, one_epoch.events,
            one_epoch.attestations, ts=one_epoch.ts,
            single_venue=one_epoch.single_venue,
        ).value
        for name, (inj, fees) in family.items():
            bp_per_usd, _ = v.measure_miner_resistance(
                miner, one_epoch, clean,
                list(one_epoch.events) + inj, fees, None,
            )
            moved[(miner.name, name)] = bp_per_usd

    # The member aimed at the mean must bite the mean harder than the member
    # aimed at the median does.
    assert moved[("naive-vwap", "out-of-band")] > 2 * moved[("naive-vwap", "in-band")]
    # And on that member the trimmed median must be the more resistant of the
    # two, which is the ordering a single in-band adversary got backwards.
    assert moved[("four-pillar", "out-of-band")] < moved[("naive-vwap", "out-of-band")]


def test_resistance_is_the_worst_case_across_the_family_not_the_average(one_epoch):
    """Worst, not mean — and the distinction is the whole fix.

    Resistance is a claim about what an attacker *can* do. Averaging lets a good
    score against the attack aimed elsewhere pay for a bad one against the attack
    aimed at you, which is exactly the credit the naive VWAP was collecting.
    Keyed on `fees` because that is a real per-member quantity.
    """
    v = Validator(attack_fn=run_optimal_attack)
    family = [("in-band", [], 1.0), ("out-of-band", [], 2.0)]
    v.measure_miner_resistance = (  # type: ignore[method-assign]
        lambda miner, ep, clean, win, fees, prev: (
            (10.0, 0.5) if fees == 1.0 else (900.0, 0.001)
        )
    )
    bp_per_usd, cost, worst = v.measure_worst_resistance(
        FourPillarMiner(uid=0), one_epoch, 1.0, family, None
    )
    assert bp_per_usd == 900.0, "the mean would be 455.0"
    assert cost == 0.001
    assert worst == "out-of-band", "the caller has to be told WHICH attack won"


def test_declining_every_member_still_reads_as_resistant(one_epoch):
    """Abstention keeps its meaning under a family.

    A miner that will not print on a tape it cannot handle is behaving correctly
    and the in-process harness has always scored it as maximally resistant. Taking
    the maximum must not turn that into a penalty — while declining *one* member
    must not excuse being moved by another, which the test above covers.
    """
    v = Validator(attack_fn=run_optimal_attack)
    family = [("in-band", [], 1.0), ("out-of-band", [], 2.0)]
    v.measure_miner_resistance = (  # type: ignore[method-assign]
        lambda miner, ep, clean, win, fees, prev: (0.0, None)
    )
    bp_per_usd, _, _ = v.measure_worst_resistance(
        FourPillarMiner(uid=0), one_epoch, 1.0, family, None
    )
    assert bp_per_usd == 0.0, "abstaining on every member is still resistance"


def test_the_probe_scales_with_the_window_and_keeps_a_floor(one_epoch):
    """A flat dollar probe cannot mean the same thing on two different windows.

    $400 was 0.33% of honest notional on windows this size, well short of what
    the bound prices the reference move at — which is why the attainability pillar
    was comparing a declared cost against a measurement taken at a budget the
    bound never predicted.
    """
    honest = sum(e.notional for e in one_epoch.events)
    v = Validator(attack_fn=run_optimal_attack)
    assert v.probe_notional(one_epoch) == pytest.approx(0.01 * honest)
    assert v.probe_notional(one_epoch) > 400.0, "the fraction must beat the old flat fee"

    # The floor still binds, so a window too thin for a fraction to buy a
    # measurable move is not probed with pennies.
    floored = Validator(attack_fn=run_optimal_attack, attack_notional=1e9)
    assert floored.probe_notional(one_epoch) == 1e9
