from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import isco_video_agent.ai_budget as ai_budget
import isco_video_agent.production_pipeline as production_pipeline
from isco_video_agent.ai_budget import AttemptOutcome, BudgetLedger, Capability, Priority, TaskSpec

from scripts import gold_thumbnail_budget
from scripts import run123_budget_closure as closure


def _fill(ledger: BudgetLedger, count: int) -> None:
    for index in range(count):
        ledger.record_attempt(
            f"seed_{index}",
            provider="seed",
            requested_model="seed",
            resolved_model="seed",
            capability=Capability.TEXT,
            outcome=AttemptOutcome.SUCCESS,
        )


class Run123BudgetEnvelopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        closure.install_run123_budget_closure()

    def test_film_envelope_is_exactly_102_with_documented_breakdown(self) -> None:
        envelope = closure.SUCCESSFUL_ATTEMPT_ENVELOPES["film"]
        self.assertEqual(
            envelope.to_dict(),
            {
                "format": "film",
                "planning": 45,
                "tts": 9,
                "vision": 32,
                "visual_recovery_text": 8,
                "director_observer": 1,
                "gold_thumbnail_p2": 4,
                "final_critic_p0": 3,
                "total": 102,
            },
        )
        self.assertEqual(ai_budget.PROVIDER_ATTEMPT_HARD_CAP["film"], 102)
        self.assertEqual(ai_budget.P1_AND_P0_RESERVED_BUFFER["film"], 3)
        self.assertEqual(
            closure.priority_ceiling(BudgetLedger("film", enforce=True), Priority.P2),
            99,
        )

    def test_story_envelope_is_exactly_69(self) -> None:
        envelope = closure.SUCCESSFUL_ATTEMPT_ENVELOPES["story"]
        self.assertEqual(
            envelope.to_dict(),
            {
                "format": "story",
                "planning": 30,
                "tts": 6,
                "vision": 20,
                "visual_recovery_text": 5,
                "director_observer": 1,
                "gold_thumbnail_p2": 4,
                "final_critic_p0": 3,
                "total": 69,
            },
        )
        self.assertEqual(ai_budget.PROVIDER_ATTEMPT_HARD_CAP["story"], 69)
        self.assertEqual(closure.priority_ceiling(BudgetLedger("story"), Priority.P2), 66)

    def test_p2_exhaustion_safe_skips_entire_thumbnail_package_before_provider_call(self) -> None:
        ledger = BudgetLedger("film", enforce=True)
        _fill(ledger, 99)  # exact recalculated P2 ceiling
        plan = Mock(format="film", title_options=["أ", "ب", "ج"], topic="موضوع")
        fallback = {
            "status": "ready",
            "budget_degraded": True,
            "budget_fallback": {"provider_attempts_consumed": 0},
            "candidates": [{}, {}, {}],
        }

        with patch.object(
            gold_thumbnail_budget,
            "_build_final_render_fallback_package",
            return_value=fallback,
        ) as build_fallback, patch.object(
            gold_thumbnail_budget.thumbnail,
            "build_thumbnail_package",
            side_effect=AssertionError("P2 provider path must not start"),
        ):
            result = gold_thumbnail_budget.build_budgeted_thumbnail_package(
                gemini_key="g",
                pexels_key="p",
                pixabay_key="x",
                plan=plan,
                output_dir=Path("unused"),
                model="gemini-2.5-flash",
                ledger=ledger,
            )

        self.assertIs(result, fallback)
        build_fallback.assert_called_once()
        self.assertEqual(ledger.to_summary()["provider_attempts"]["total"], 99)
        skipped = ledger.to_summary()["p2_skipped"]
        self.assertEqual(len(skipped), 4)
        self.assertIn("GOLD_SHADOW_THUMBNAIL_CONCEPTS", skipped)

    def test_less_than_four_p2_slots_also_skips_before_partial_package(self) -> None:
        ledger = BudgetLedger("film", enforce=True)
        _fill(ledger, 97)  # two P2 slots remain; package needs all four atomically
        self.assertEqual(closure.remaining_priority_capacity(ledger, Priority.P2), 2)
        with patch.object(
            gold_thumbnail_budget,
            "_build_final_render_fallback_package",
            return_value={"status": "ready", "budget_degraded": True, "candidates": [{}, {}, {}]},
        ), patch.object(
            gold_thumbnail_budget.thumbnail,
            "build_thumbnail_package",
            side_effect=AssertionError("must not start a partial P2 package"),
        ):
            gold_thumbnail_budget.build_budgeted_thumbnail_package(
                gemini_key="g",
                pexels_key="p",
                plan=Mock(format="film"),
                output_dir=Path("unused"),
                model="gemini-2.5-flash",
                ledger=ledger,
            )
        self.assertEqual(ledger.to_summary()["provider_attempts"]["total"], 97)

    def test_enforced_final_critic_is_p0_and_owns_the_last_three_slots(self) -> None:
        ledger = BudgetLedger("film", enforce=True)
        _fill(ledger, 99)
        with closure.enforcing_final_critic_as_p0():
            opening = production_pipeline._final_critic_spec(
                "GOLD_FINAL_CRITIC_OPENING_VISUAL", Capability.VISION
            )
            release = production_pipeline._final_critic_spec(
                "GOLD_FINAL_CRITIC_RELEASE_REVIEW", Capability.TEXT
            )
            self.assertIs(opening.priority, Priority.P0)
            self.assertIs(release.priority, Priority.P0)

            # One opening-Vision call plus two release-text provider attempts exactly
            # fill attempts 100, 101 and 102. The next P0 attempt is refused.
            for spec in (opening, release, replace_max_attempts(release, 2)):
                ledger.register_task(spec)
                self.assertTrue(ledger.authorize(spec.task_id))
                ledger.record_attempt(
                    spec.task_id,
                    provider="gemini",
                    requested_model="m",
                    resolved_model="m",
                    capability=spec.capability,
                    outcome=AttemptOutcome.SUCCESS,
                )

        self.assertEqual(ledger.to_summary()["provider_attempts"]["total"], 102)
        overflow = TaskSpec(
            task_id="AFTER_RELEASE_OVERFLOW",
            kind="AFTER_RELEASE_OVERFLOW",
            priority=Priority.P0,
            capability=Capability.TEXT,
            max_provider_attempts=1,
            schema_repair_allowed=False,
            local_fallback=False,
            semantic_block_is_final=False,
        )
        ledger.register_task(overflow)
        self.assertFalse(ledger.authorize(overflow.task_id))


def replace_max_attempts(spec: TaskSpec, value: int) -> TaskSpec:
    return TaskSpec(
        task_id=spec.task_id,
        kind=spec.kind,
        priority=spec.priority,
        capability=spec.capability,
        max_provider_attempts=value,
        schema_repair_allowed=spec.schema_repair_allowed,
        local_fallback=spec.local_fallback,
        semantic_block_is_final=spec.semantic_block_is_final,
    )


if __name__ == "__main__":
    unittest.main()
