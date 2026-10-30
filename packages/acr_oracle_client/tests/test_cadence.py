"""How press runs are read out of PricePosted events.

The whole reason this lives in a package rather than in the script: two callers
need the SAME answer from different places — `scripts/print_gaps.py` from a
laptop, the Terminal's systems ledger from inside the press — and `scripts/` is
not in the Docker image. These tests pin the behaviour both depend on.
"""

from __future__ import annotations

from acr_oracle_client.cadence import (
    RUN_COALESCE_S,
    index_gaps_min,
    press_runs,
    run_gaps_min,
)


def _post(at_wall: float, index_id: str) -> dict:
    return {"at_wall": at_wall, "index_id": index_id, "tx": "0x", "block": 1, "signer": "0x"}


class TestPressRuns:
    def test_one_run_per_visit_not_one_per_index(self):
        # The bug this function exists to prevent: the poster writes three
        # prints (one per index) a few seconds apart, so diffing raw event
        # timestamps yields a pile of 0.1-minute "gaps" and hides the real hole.
        posts = [_post(1000.0, "ACR-INF"), _post(1002.0, "ACR-GPU"), _post(1004.0, "ACR-DATA")]
        runs = press_runs(posts)
        assert len(runs) == 1
        assert runs[0][1] == {"ACR-INF", "ACR-GPU", "ACR-DATA"}

    def test_a_genuine_gap_stays_two_runs(self):
        posts = [_post(1000.0, "ACR-INF"), _post(1000.0 + 3600, "ACR-INF")]
        assert len(press_runs(posts)) == 2

    def test_the_coalesce_boundary_is_exclusive(self):
        # Strictly inside the window is one run; exactly at it is two, so the
        # constant means what it says.
        assert len(press_runs([_post(0.0, "A"), _post(RUN_COALESCE_S - 1, "B")])) == 1
        assert len(press_runs([_post(0.0, "A"), _post(RUN_COALESCE_S, "B")])) == 2

    def test_runs_come_out_oldest_first_whatever_the_input_order(self):
        posts = [_post(3000.0, "A"), _post(1000.0, "A"), _post(2000.0, "A")]
        assert [r[0] for r in press_runs(posts)] == [1000.0, 2000.0, 3000.0]

    def test_no_posts_is_no_runs(self):
        assert press_runs([]) == []


class TestGaps:
    def test_run_gaps_are_minutes(self):
        runs = press_runs([_post(0.0, "A"), _post(3600.0, "A"), _post(7200.0, "A")])
        assert run_gaps_min(runs) == [60.0, 60.0]

    def test_since_excludes_earlier_runs_from_judgement(self):
        # A pre-fix tape can be printed without being judged — that is what
        # --since is for in print_gaps.py.
        runs = press_runs([_post(0.0, "A"), _post(20_000.0, "A"), _post(23_600.0, "A")])
        assert run_gaps_min(runs) == [333.3333333333333, 60.0]
        assert run_gaps_min(runs, since=23_000.0) == [60.0]

    def test_one_run_has_no_gap_to_measure(self):
        assert run_gaps_min(press_runs([_post(0.0, "A")])) == []

    def test_per_index_gaps_see_a_skipped_index_the_run_cadence_hides(self):
        # ACR-INF is pressed every run; ACR-GPU only every other one. A series
        # settles against ITS index, so GPU's real gap is double — and the run
        # cadence would report both as healthy.
        posts = [
            _post(0.0, "ACR-INF"), _post(0.0, "ACR-GPU"),
            _post(3600.0, "ACR-INF"),
            _post(7200.0, "ACR-INF"), _post(7200.0, "ACR-GPU"),
        ]
        assert index_gaps_min(posts, "ACR-INF") == [60.0, 60.0]
        assert index_gaps_min(posts, "ACR-GPU") == [120.0]

    def test_an_index_with_one_print_has_no_gap(self):
        assert index_gaps_min([_post(0.0, "ACR-INF")], "ACR-INF") == []

    def test_an_unknown_index_is_empty_not_an_error(self):
        assert index_gaps_min([_post(0.0, "ACR-INF")], "ACR-NOPE") == []
