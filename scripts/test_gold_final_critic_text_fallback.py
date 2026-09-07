from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import isco_video_agent.ai_budget as ai_budget
import isco_video_agent.final_critic as final_critic
from isco_video_agent.ai_budget import BudgetLedger, Capability, Priority, TaskSpec

import scripts.gold_final_critic_text_fallback as fallback
from scripts import provider_health_registry as health


def _release_spec() -> TaskSpec:
    return TaskSpec(
        task_id="GOLD_FINAL_CRITIC_RELEASE_REVIEW",
        kind="GOLD_FINAL_CRITIC",
        priority=Priority.P0,
        capability=Capability.TEXT,
        max_provider_attempts=1,
        schema_repair_allowed=False,
        local_fallback=False,
        semantic_block_is_final=True,
    )


def _opening_spec() -> TaskSpec:
    return TaskSpec(
        task_id="GOLD_FINAL_CRITIC_OPENING_VISUAL",
        kind="GOLD_FINAL_CRITIC",
        priority=Priority.P0,
        capability=Capability.VISION,
        max_provider_attempts=1,
        schema_repair_allowed=False,
        local_fallback=False,
        semantic_block_is_final=True,
    )


def _critic_like_call(*_args, **_kwargs):
    try:
        raw = final_critic.json_text("gemini-key", "critic prompt", model="gemini-2.5-flash")
    except Exception:
        return {
            "status": "block",
            "model_review": {
                "status": "block",
                "critical_issues": ["Final critic could not complete safely"],
            },
        }
    return {
        "status": str(raw.get("status", "pass")),
        "model_review": {"status": str(raw.get("status", "pass"))},
    }


def _preview(root: str) -> Path:
    path = Path(root) / "opening-preview.mp4"
    path.write_bytes(b"final-critic-preview")
    return path


class GoldFinalCriticProviderMeshTests(unittest.TestCase):
    def setUp(self) -> None:
        health.reset_provider_health()
        fallback.vision_mesh._GROQ_MODEL_CERTIFIED.set(None)

    def test_gemini_timeout_switches_once_to_openrouter_and_accounts_both(self) -> None:
        ledger = BudgetLedger("film", enforce=True)
        original_status = Mock(side_effect=AssertionError("release review must use fallback path"))
        with patch.object(final_critic, "json_text", side_effect=TimeoutError("request timed out")) as gemini, patch.object(
            fallback, "openrouter_json_text", return_value={"status": "pass"}
        ) as openrouter:
            result = fallback._release_review_with_fallback(
                original_status,
                ledger,
                _release_spec(),
                "gemini",
                "gemini-2.5-flash",
                _critic_like_call,
            )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(gemini.call_count, 1)
        self.assertEqual(openrouter.call_count, 1)
        summary = ledger.to_summary()
        self.assertEqual(summary["provider_attempts"]["total"], 2)
        self.assertEqual(summary["provider_attempts"]["by_provider"], {"gemini": 1, "openrouter": 1})
        self.assertEqual(summary["provider_attempts"]["by_outcome"], {"TIMEOUT": 1, "SUCCESS": 1})

    def test_text_semantic_block_is_final_and_never_shops_openrouter(self) -> None:
        ledger = BudgetLedger("film", enforce=True)
        original_status = Mock(side_effect=AssertionError("release review must use fallback path"))
        with patch.object(final_critic, "json_text", return_value={"status": "block"}) as gemini, patch.object(
            fallback, "openrouter_json_text", side_effect=AssertionError("semantic block must not fallback")
        ) as openrouter:
            result = fallback._release_review_with_fallback(
                original_status,
                ledger,
                _release_spec(),
                "gemini",
                "gemini-2.5-flash",
                _critic_like_call,
            )

        self.assertEqual(result["status"], "block")
        self.assertEqual(gemini.call_count, 1)
        self.assertEqual(openrouter.call_count, 0)
        self.assertEqual(
            ledger.to_summary()["provider_attempts"]["by_outcome"],
            {"CONTENT_BLOCKED": 1},
        )

    def test_openrouter_text_failure_stops_after_exactly_two_attempts(self) -> None:
        ledger = BudgetLedger("film", enforce=True)
        with patch.object(final_critic, "json_text", side_effect=RuntimeError("Gemini HTTP 503")), patch.object(
            fallback, "openrouter_json_text", side_effect=RuntimeError("OpenRouter HTTP 503")
        ):
            result = fallback._release_review_with_fallback(
                Mock(side_effect=AssertionError("release review must use fallback path")),
                ledger,
                _release_spec(),
                "gemini",
                "gemini-2.5-flash",
                _critic_like_call,
            )

        self.assertEqual(result["status"], "block")
        self.assertEqual(ledger.to_summary()["provider_attempts"]["total"], 2)

    def test_opening_vision_enters_existing_run181_mesh_with_four_attempt_task_cap(self) -> None:
        ledger = BudgetLedger("film", enforce=True)
        expected = {"status": "pass"}
        with patch.object(
            fallback.vision_mesh,
            "_route_visual_audit_v3",
            return_value=expected,
        ) as route:
            result = fallback._opening_vision_with_mesh(
                Mock(side_effect=AssertionError("Gold opening must use shared Vision mesh")),
                ledger,
                _opening_spec(),
                "gemini",
                "gemini-3.7-flash",
                Mock(),
                "gem-key",
                Path("opening-preview.mp4"),
                narration_context="ctx",
                intended_visual="intent",
            )

        self.assertIs(result, expected)
        routed_spec = route.call_args.args[1]
        self.assertEqual(routed_spec.task_id, "GOLD_FINAL_CRITIC_OPENING_VISUAL")
        self.assertEqual(routed_spec.kind, "VISUAL_AUDIT")
        self.assertEqual(routed_spec.max_provider_attempts, 4)
        self.assertTrue(routed_spec.semantic_block_is_final)
        self.assertIs(routed_spec.priority, Priority.P0)

    def test_short_provider_retry_after_is_honored_once_and_accounted(self) -> None:
        ledger = BudgetLedger("film", enforce=True)
        gemini = Mock(
            side_effect=[
                RuntimeError("429 RESOURCE_EXHAUSTED Please retry in 0.01s"),
                {"status": "pass"},
            ]
        )
        with tempfile.TemporaryDirectory() as root, fallback.vision_mesh.contract.legacy.vision_provider_circuit_scope(), patch.object(
            fallback.vision_mesh,
            "refresh_runtime_provider_health",
            return_value=None,
        ), patch.object(
            fallback.vision_mesh,
            "_run_groq_attempt",
            side_effect=AssertionError("successful Gemini retry must not fall through to Groq"),
        ), patch.object(
            fallback.time,
            "sleep",
        ) as sleep:
            result = fallback._opening_vision_with_mesh(
                Mock(side_effect=AssertionError("must use Vision mesh")),
                ledger,
                _opening_spec(),
                "gemini",
                "gemini-3.7-flash",
                gemini,
                "gem-key",
                _preview(root),
                narration_context="ctx",
                intended_visual="intent",
            )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(gemini.call_count, 2)
        sleep.assert_called_once_with(0.01)
        summary = ledger.to_summary()
        self.assertEqual(summary["provider_attempts"]["total"], 2)
        self.assertEqual(summary["provider_attempts"]["by_provider"], {"gemini": 2})
        self.assertEqual(summary["provider_attempts"]["by_outcome"], {"RATE_LIMITED": 1, "SUCCESS": 1})

    def test_retry_after_above_budget_fails_over_to_groq_without_sleeping(self) -> None:
        ledger = BudgetLedger("film", enforce=True)
        gemini = Mock(side_effect=RuntimeError("429 RESOURCE_EXHAUSTED Please retry in 45s"))
        with tempfile.TemporaryDirectory() as root, fallback.vision_mesh.contract.legacy.vision_provider_circuit_scope(), patch.object(
            fallback.vision_mesh,
            "refresh_runtime_provider_health",
            return_value=None,
        ), patch.object(
            fallback.vision_mesh,
            "_groq_visual_call",
            return_value={"status": "pass"},
        ) as groq_wire, patch.object(
            fallback.time,
            "sleep",
        ) as sleep:
            result = fallback._opening_vision_with_mesh(
                Mock(side_effect=AssertionError("must use Vision mesh")),
                ledger,
                _opening_spec(),
                "gemini",
                "gemini-3.7-flash",
                gemini,
                "gem-key",
                _preview(root),
                narration_context="ctx",
                intended_visual="intent",
            )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(gemini.call_count, 1)
        groq_wire.assert_called_once()
        sleep.assert_not_called()
        summary = ledger.to_summary()
        self.assertEqual(summary["provider_attempts"]["total"], 2)
        self.assertEqual(summary["provider_attempts"]["by_provider"], {"gemini": 1, "groq": 1})

    def test_failed_gemini_retry_then_groq_success_counts_three_real_calls(self) -> None:
        ledger = BudgetLedger("film", enforce=True)
        gemini = Mock(
            side_effect=[
                RuntimeError("429 RESOURCE_EXHAUSTED Please retry in 0s"),
                RuntimeError("Gemini HTTP 503"),
            ]
        )
        with tempfile.TemporaryDirectory() as root, fallback.vision_mesh.contract.legacy.vision_provider_circuit_scope(), patch.object(
            fallback.vision_mesh,
            "refresh_runtime_provider_health",
            return_value=None,
        ), patch.object(
            fallback.vision_mesh,
            "_groq_visual_call",
            return_value={"status": "pass"},
        ) as groq_wire, patch.object(fallback.time, "sleep"):
            result = fallback._opening_vision_with_mesh(
                Mock(side_effect=AssertionError("must use Vision mesh")),
                ledger,
                _opening_spec(),
                "gemini",
                "gemini-3.7-flash",
                gemini,
                "gem-key",
                _preview(root),
                narration_context="ctx",
                intended_visual="intent",
            )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(gemini.call_count, 2)
        groq_wire.assert_called_once()
        summary = ledger.to_summary()
        self.assertEqual(summary["provider_attempts"]["total"], 3)
        self.assertEqual(summary["provider_attempts"]["by_provider"], {"gemini": 2, "groq": 1})

    def test_visual_semantic_block_is_final_and_never_shops_groq_or_openrouter(self) -> None:
        ledger = BudgetLedger("film", enforce=True)
        block = {"status": "block"}
        with tempfile.TemporaryDirectory() as root, fallback.vision_mesh.contract.legacy.vision_provider_circuit_scope(), patch.object(
            fallback.vision_mesh,
            "refresh_runtime_provider_health",
            return_value=None,
        ), patch.object(
            fallback.vision_mesh,
            "_run_groq_attempt",
        ) as groq, patch.object(
            fallback.vision_mesh.contract,
            "_run_openrouter_attempt",
        ) as openrouter:
            result = fallback._opening_vision_with_mesh(
                Mock(side_effect=AssertionError("must use Vision mesh")),
                ledger,
                _opening_spec(),
                "gemini",
                "gemini-3.7-flash",
                Mock(return_value=block),
                "gem-key",
                _preview(root),
                narration_context="ctx",
                intended_visual="intent",
            )

        self.assertEqual(result["status"], "block")
        groq.assert_not_called()
        openrouter.assert_not_called()
        self.assertTrue(ledger.is_task_closed("GOLD_FINAL_CRITIC_OPENING_VISUAL"))

    def test_release_budget_expands_only_inside_scope_and_restores_exact_state(self) -> None:
        hard_before = dict(ai_budget.PROVIDER_ATTEMPT_HARD_CAP)
        reserve_before = dict(ai_budget.P1_AND_P0_RESERVED_BUFFER)
        baseline_attempts = int(fallback.run123._FINAL_CRITIC_PROVIDER_ATTEMPTS)
        expected_delta = max(0, fallback._FINAL_CRITIC_TOTAL_PROVIDER_ATTEMPTS - baseline_attempts)

        with fallback._final_critic_provider_budget_scope():
            for fmt, baseline_cap in fallback.run123.RUN123_PROVIDER_ATTEMPT_HARD_CAP.items():
                self.assertGreaterEqual(
                    ai_budget.PROVIDER_ATTEMPT_HARD_CAP[fmt],
                    int(baseline_cap) + expected_delta,
                )
                self.assertGreaterEqual(
                    ai_budget.P1_AND_P0_RESERVED_BUFFER[fmt],
                    fallback._FINAL_CRITIC_TOTAL_PROVIDER_ATTEMPTS,
                )

        self.assertEqual(ai_budget.PROVIDER_ATTEMPT_HARD_CAP, hard_before)
        self.assertEqual(ai_budget.P1_AND_P0_RESERVED_BUFFER, reserve_before)

    def test_context_manager_restores_engine_ledger_wrapper_and_budget_state(self) -> None:
        import isco_video_agent.production_pipeline as pipeline

        original = pipeline._ledger_call_status
        hard_before = dict(ai_budget.PROVIDER_ATTEMPT_HARD_CAP)
        reserve_before = dict(ai_budget.P1_AND_P0_RESERVED_BUFFER)
        with fallback.gold_final_critic_text_fallback():
            self.assertIsNot(pipeline._ledger_call_status, original)
            self.assertNotEqual(ai_budget.PROVIDER_ATTEMPT_HARD_CAP, hard_before)
        self.assertIs(pipeline._ledger_call_status, original)
        self.assertEqual(ai_budget.PROVIDER_ATTEMPT_HARD_CAP, hard_before)
        self.assertEqual(ai_budget.P1_AND_P0_RESERVED_BUFFER, reserve_before)


if __name__ == "__main__":
    unittest.main()
