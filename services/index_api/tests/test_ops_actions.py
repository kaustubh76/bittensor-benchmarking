"""The operator console's guardrails.

A UI is not a security boundary, so every rule that matters is asserted here
against the module the routes call — not against a form.
"""

from __future__ import annotations

import json

import pytest
from index_api import ops_actions
from index_api.ops_actions import ActionError


@pytest.fixture(autouse=True)
def _audit(tmp_path, monkeypatch):
    monkeypatch.setattr(ops_actions, "AUDIT_PATH", str(tmp_path / "ops.jsonl"))
    monkeypatch.delenv("ACR_OPS_TOKEN", raising=False)


class TestAuth:
    def test_the_console_is_off_unless_a_token_is_configured(self):
        assert ops_actions.enabled() is False

    def test_no_token_configured_reports_not_found_never_unauthorized(self):
        # 404, not 401. An endpoint that admits it exists is one worth
        # guessing at, and the overwhelming majority of deploys want none of
        # this surface at all.
        with pytest.raises(ActionError) as e:
            ops_actions.authorize("anything")
        assert e.value.status == 404

    def test_a_wrong_key_is_rejected(self, monkeypatch):
        monkeypatch.setenv("ACR_OPS_TOKEN", "correct-horse")
        with pytest.raises(ActionError) as e:
            ops_actions.authorize("wrong")
        assert e.value.status == 401

    def test_a_missing_key_is_rejected_even_when_enabled(self, monkeypatch):
        monkeypatch.setenv("ACR_OPS_TOKEN", "correct-horse")
        with pytest.raises(ActionError) as e:
            ops_actions.authorize(None)
        assert e.value.status == 401

    def test_the_right_key_passes(self, monkeypatch):
        monkeypatch.setenv("ACR_OPS_TOKEN", "correct-horse")
        ops_actions.authorize("correct-horse")  # no raise


class TestDryRunIsTheDefault:
    def test_a_keeper_nudge_dry_runs_without_touching_state(self, monkeypatch):
        from index_api import keeper

        monkeypatch.setattr(keeper, "_last_heartbeat", 1234.0)
        out = ops_actions.run("keeper/heartbeat", {}, True)
        assert out["dry_run"] is True
        assert "would" in out["result"]
        # The whole point: a dry run changed nothing.
        assert keeper._last_heartbeat == 1234.0

    def test_executing_requires_saying_so(self, monkeypatch):
        from index_api import keeper

        monkeypatch.setattr(keeper, "_last_heartbeat", 1234.0)
        ops_actions.run("keeper/heartbeat", {}, False)
        assert keeper._last_heartbeat == 0.0


class TestCaps:
    def test_collateral_is_capped_server_side(self, monkeypatch):
        monkeypatch.setattr(ops_actions, "MAX_COLLATERALIZE_USDC", 2.0)
        with pytest.raises(ActionError) as e:
            ops_actions.run("venue/collateralize", {"usdc": 50, "series_id": 1}, True)
        assert e.value.status == 400
        assert "capped at 2.00" in str(e.value)

    def test_a_transfer_is_capped_server_side(self, monkeypatch):
        monkeypatch.setattr(ops_actions, "MAX_FUND_USDC", 5.0)
        with pytest.raises(ActionError) as e:
            ops_actions.run("funding/move", {"role": "maker", "usdc": 999}, True)
        assert e.value.status == 400

    def test_a_negative_amount_is_refused(self):
        with pytest.raises(ActionError):
            ops_actions.run("funding/move", {"role": "maker", "usdc": -1}, True)

    def test_an_unknown_role_is_refused(self):
        with pytest.raises(ActionError) as e:
            ops_actions.run("funding/move", {"role": "treasury", "usdc": 1}, True)
        assert e.value.status == 400


class TestPause:
    def test_pausing_requires_the_word(self):
        # A boolean the UI could send by accident must not halt a live venue.
        with pytest.raises(ActionError) as e:
            ops_actions.run("venue/pause", {"paused": True}, True)
        assert e.value.status == 400

    def test_the_dry_run_names_the_wallet_that_would_sign(self, monkeypatch):
        # setPaused is onlyOwner. A dry run that skips the signer check is worse
        # than no dry run: it shows green, and the operator learns the venue is
        # owned by someone else from a reverted transaction, ninety seconds into
        # the incident this control exists for.
        class _Sg:
            address = "0xOWNER"

        monkeypatch.setattr(
            ops_actions, "_owner_signer", lambda: (object(), _Sg(), "maker", "0xOWNER")
        )
        out = ops_actions.run("venue/pause", {"paused": True, "confirm": "pause"}, True)
        assert "would" in out["result"]
        assert out["result"]["owner_on_chain"] == "0xOWNER"
        assert "maker" in out["result"]["signed_by"]

    def test_the_dry_run_refuses_when_no_configured_wallet_owns_the_venue(self, monkeypatch):
        # Ownership is a fact on-chain, and it has moved before. When it moves
        # again the emergency stop must say so up front, not revert.
        def _boom():
            raise ActionError(503, "no configured wallet owns this venue (owner 0xabc…)")

        monkeypatch.setattr(ops_actions, "_owner_signer", _boom)
        with pytest.raises(ActionError) as e:
            ops_actions.run("venue/pause", {"paused": True, "confirm": "pause"}, True)
        assert e.value.status == 503
        assert "owns this venue" in str(e.value)


class TestRegistry:
    def test_every_promised_action_is_registered(self):
        # Asserting only that dangerous actions are ABSENT is how `venue/withdraw`
        # went missing for a whole round: the console could `venue/roll`, which
        # strands collateral on the retired series, and had no way to reclaim it.
        # A negative-only test cannot catch an omission.
        assert set(ops_actions.ACTIONS) == {
            "keeper/heartbeat",
            "keeper/roll-check",
            "verify/run",
            "venue/settle",
            "venue/roll",
            "venue/collateralize",
            "venue/withdraw",
            "funding/move",
            "venue/pause",
            "operator/approve",
            "operator/reject",
        }

    def test_every_action_has_a_handler_and_a_description(self):
        for name, entry in ops_actions.ACTIONS.items():
            fn, desc = entry
            assert callable(fn), name
            assert isinstance(desc, str) and desc.strip(), name

    def test_the_registry_is_the_allowlist(self):
        with pytest.raises(ActionError) as e:
            ops_actions.run("venue/transferOwnership", {}, True)
        assert e.value.status == 404

    def test_ownership_and_signer_changes_are_not_reachable_at_all(self):
        # Deliberately absent: these change who controls the system rather
        # than what it is doing, and a bearer token is not the right key.
        for name in ("setSigner", "transferOwnership", "venue/openSeries"):
            assert name not in ops_actions.ACTIONS


class TestAudit:
    def test_a_refusal_is_written_down_too(self):
        with pytest.raises(ActionError):
            ops_actions.run("funding/move", {"role": "maker", "usdc": 999}, True)
        rows = ops_actions.recent()
        assert len(rows) == 1
        assert rows[0]["ok"] is False
        assert rows[0]["action"] == "funding/move"
        assert rows[0]["error"]

    def test_a_success_records_the_result_and_the_dry_run_flag(self):
        ops_actions.run("keeper/heartbeat", {}, True)
        row = ops_actions.recent()[0]
        assert row["ok"] is True and row["dry_run"] is True

    def test_the_newest_entry_comes_first(self):
        ops_actions.run("keeper/heartbeat", {}, True)
        ops_actions.run("keeper/roll-check", {}, True)
        assert ops_actions.recent()[0]["action"] == "keeper/roll-check"

    def test_the_log_is_valid_jsonl(self):
        ops_actions.run("keeper/heartbeat", {}, True)
        with open(ops_actions.AUDIT_PATH, encoding="utf-8") as fh:
            for line in fh:
                json.loads(line)

    def test_an_empty_log_reads_as_nothing_not_a_crash(self):
        assert ops_actions.recent() == []


class TestBadKeyThrottle:
    """Guessing is bounded; a valid key is never throttled by someone else."""

    @pytest.fixture(autouse=True)
    def _fresh_bucket(self, monkeypatch):
        from index_api import ratelimit

        monkeypatch.setattr(ratelimit, "_limiter", ratelimit.RateLimiter())
        monkeypatch.setenv("ACR_OPS_TOKEN", "correct-horse-battery")
        monkeypatch.setattr(ops_actions, "MAX_BAD_KEYS", 3)

    def test_repeated_wrong_keys_stop_being_answered(self):
        for i in range(3):
            with pytest.raises(ActionError) as e:
                ops_actions.authorize(f"guess-{i}")
            assert e.value.status == 401
        with pytest.raises(ActionError) as e:
            ops_actions.authorize("guess-4")
        assert e.value.status == 429

    def test_rotating_the_guess_does_not_dodge_the_bucket(self):
        # The reason the bucket is keyed on nothing: a per-token counter would
        # never trip, because every guess is a different token.
        for i in range(3):
            with pytest.raises(ActionError):
                ops_actions.authorize(f"unique-guess-{i}")
        with pytest.raises(ActionError) as e:
            ops_actions.authorize("yet-another-different-one")
        assert e.value.status == 429

    def test_the_real_key_still_works_while_guessers_are_throttled(self):
        # The failure this design exists to prevent: a stranger's noise must not
        # lock the operator out of their own emergency controls.
        for i in range(9):
            with pytest.raises(ActionError):
                ops_actions.authorize(f"guess-{i}")
        ops_actions.authorize("correct-horse-battery")  # no raise

    def test_a_correct_key_never_consumes_the_bucket(self):
        for _ in range(20):
            ops_actions.authorize("correct-horse-battery")
        with pytest.raises(ActionError) as e:
            ops_actions.authorize("wrong")
        assert e.value.status == 401  # still answering, not 429
