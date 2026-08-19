from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from isco_video_agent.ai_budget import AttemptOutcome, BudgetLedger, Capability, Priority, TaskSpec
import scripts.gold_shadow_phase2a as shadow


def _gold_result(status: str = "pass", *, hard_blocks: list[str] | None = None) -> tuple[object, dict, dict]:
    critic = {
        "status": status,
        "observation_status": "ok",
        "hard_blocks": list(hard_blocks or []),
        "model_review": {"status": status, "summary": status},
    }
    state = {
        "would_accept": status == "pass",
        "would_reject": status != "pass",
        "would_mark_production_accepted": status == "pass",
        "would_remove_history_record": status != "pass",
        "would_sync_state_snapshot": status != "pass",
        "state_mutation_performed": False,
        "thumbnail_enabled": False,
        "rights_augmentation_enabled": False,
        "postpublish_learning_enabled": False,
    }
    return SimpleNamespace(format="film"), critic, {"state_semantics": state, "critic": critic}


class GoldShadowPhase2ARunnerTests(unittest.TestCase):
    def test_same_ledger_is_forwarded_and_shadow_attempt_delta_is_observed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "final.mp4").write_bytes(b"one-render")
            state = out / "history.json"
            ledger = BudgetLedger("film", enforce=False)
            seen = {}

            def observe(**kwargs):
                seen.update(kwargs)
                spec = TaskSpec(
                    task_id="GOLD_SHADOW_TEST",
                    kind="GOLD_SHADOW_FINAL_CRITIC",
                    priority=Priority.P2,
                    capability=Capability.TEXT,
                    max_provider_attempts=1,
                    schema_repair_allowed=False,
                    local_fallback=False,
                    semantic_block_is_final=True,
                )
                ledger.register_task(spec)
                ledger.record_attempt(
                    spec.task_id,
                    provider="gemini",
                    requested_model="gemini-2.5-flash",
                    resolved_model="gemini-2.5-flash",
                    capability=Capability.TEXT,
                    outcome=AttemptOutcome.SUCCESS,
                )
                return _gold_result("pass")

            with patch.object(shadow, "history_path", return_value=state), \
                 patch.object(shadow, "observe_gold_output", side_effect=observe):
                result = shadow.run_gold_shadow_phase2a(
                    output_dir=out,
                    gemini="fake-key",
                    ledger=ledger,
                    legacy_critic={"status": "pass", "hard_blocks": [], "observation_status": "ok"},
                    plan_from_json=lambda _path: object(),
                    run_final_critic=lambda **_kwargs: {},
                )

            self.assertIs(seen["ledger"], ledger)
            self.assertEqual(seen["report_dir"], out / "gold-shadow" / "phase2a")
            self.assertTrue(result["budget"]["same_ledger"])
            self.assertEqual(result["budget"]["gold_shadow_provider_attempt_delta"], 1)
            self.assertFalse(result["same_render"]["artifact_divergence"])
            self.assertFalse(result["state_observation"]["state_mutation_detected"])

    def test_observer_exception_is_contained_and_release_authority_stays_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "final.mp4").write_bytes(b"one-render")
            with patch.object(shadow, "history_path", return_value=out / "missing-history.json"), \
                 patch.object(shadow, "observe_gold_output", side_effect=RuntimeError("boom")):
                result = shadow.run_gold_shadow_phase2a(
                    output_dir=out,
                    gemini="fake-key",
                    ledger=BudgetLedger("film", enforce=False),
                    legacy_critic={"status": "pass", "hard_blocks": [], "observation_status": "ok"},
                    plan_from_json=lambda _path: object(),
                    run_final_critic=lambda **_kwargs: {},
                )

            self.assertEqual(result["release_authority"], "legacy_v4")
            self.assertEqual(result["gold_shadow"]["observation_status"], "failed_observation")
            self.assertTrue(result["gold_shadow"]["state_semantics"]["would_reject"])
            self.assertFalse(result["gold_shadow"]["state_semantics"]["state_mutation_performed"])
            self.assertFalse(result["same_render"]["artifact_divergence"])

    def test_artifact_and_state_mutation_are_detected_but_do_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            final_path = out / "final.mp4"
            final_path.write_bytes(b"before")
            state = out / "history.json"
            state.write_text('{"before": true}', encoding="utf-8")

            def mutate(**_kwargs):
                final_path.write_bytes(b"after")
                state.write_text('{"after": true}', encoding="utf-8")
                return _gold_result("pass")

            with patch.object(shadow, "history_path", return_value=state), \
                 patch.object(shadow, "observe_gold_output", side_effect=mutate):
                result = shadow.run_gold_shadow_phase2a(
                    output_dir=out,
                    gemini="fake-key",
                    ledger=BudgetLedger("film", enforce=False),
                    legacy_critic={"status": "pass", "hard_blocks": [], "observation_status": "ok"},
                    plan_from_json=lambda _path: object(),
                    run_final_critic=lambda **_kwargs: {},
                )

            self.assertTrue(result["same_render"]["artifact_divergence"])
            self.assertTrue(result["state_observation"]["state_mutation_detected"])
            self.assertTrue(result["divergences"]["artifact_divergence"])
            self.assertTrue(result["divergences"]["state_semantics_divergence"])
            self.assertTrue((out / "gold-shadow-comparison.json").exists())

    def test_deterministic_policy_divergence_is_classified_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "final.mp4").write_bytes(b"one-render")
            with patch.object(shadow, "history_path", return_value=out / "missing-history.json"), \
                 patch.object(
                     shadow,
                     "observe_gold_output",
                     return_value=_gold_result("block", hard_blocks=["opening_visual_audit_failed"]),
                 ):
                result = shadow.run_gold_shadow_phase2a(
                    output_dir=out,
                    gemini="fake-key",
                    ledger=BudgetLedger("film", enforce=False),
                    legacy_critic={"status": "block", "hard_blocks": ["duration_gate_failed"], "observation_status": "ok"},
                    plan_from_json=lambda _path: object(),
                    run_final_critic=lambda **_kwargs: {},
                )

            self.assertTrue(result["divergences"]["deterministic_policy_divergence"])
            self.assertFalse(result["divergences"]["verdict_divergence"])
            stored = json.loads((out / "gold-shadow-comparison.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["release_authority"], "legacy_v4")


if __name__ == "__main__":
    unittest.main()
