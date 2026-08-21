from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import isco_video_agent.final_critic as final_critic
from isco_video_agent.ai_budget import BudgetLedger, Capability, Priority, TaskSpec

import scripts.gold_final_critic_text_fallback as fallback


def _release_spec() -> TaskSpec:
    return TaskSpec(
        task_id="GOLD_FINAL_CRITIC_RELEASE_REVIEW",
        kind="GOLD_FINAL_CRITIC",
        priority=Priority.P2,
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
        priority=Priority.P2,
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


class GoldFinalCriticTextFallbackTests(unittest.TestCase):
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

    def test_semantic_block_is_final_and_never_shops_openrouter(self) -> None:
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
        summary = ledger.to_summary()
        self.assertEqual(summary["provider_attempts"]["total"], 1)
        self.assertEqual(summary["provider_attempts"]["by_outcome"], {"CONTENT_BLOCKED": 1})

    def test_openrouter_failure_stops_after_exactly_two_attempts(self) -> None:
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
        summary = ledger.to_summary()
        self.assertEqual(summary["provider_attempts"]["total"], 2)
        self.assertEqual(summary["provider_attempts"]["by_provider"], {"gemini": 1, "openrouter": 1})

    def test_opening_vision_delegates_to_original_gemini_path(self) -> None:
        ledger = BudgetLedger("film", enforce=True)
        expected = {"status": "pass"}
        original_status = Mock(return_value=expected)
        result = fallback._release_review_with_fallback(
            original_status,
            ledger,
            _opening_spec(),
            "gemini",
            "gemini-2.5-flash",
            Mock(),
        )
        self.assertIs(result, expected)
        original_status.assert_called_once()

    def test_context_manager_restores_engine_ledger_wrapper(self) -> None:
        import isco_video_agent.production_pipeline as pipeline

        original = pipeline._ledger_call_status
        with fallback.gold_final_critic_text_fallback():
            self.assertIsNot(pipeline._ledger_call_status, original)
        self.assertIs(pipeline._ledger_call_status, original)


if __name__ == "__main__":
    unittest.main()
