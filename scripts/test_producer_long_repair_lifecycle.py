from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from isco_video_agent.ai_budget import BudgetLedger, Priority
from isco_video_agent.models import ProductionPlan, ScriptSection

from scripts import producer_planning_lifecycle as lifecycle
from scripts.producer_quality_contract import ProducerQualityContractError


class ProducerLongRepairLifecycleTests(unittest.TestCase):
    def _long(
        self,
        *,
        key_points: list[str],
        visual_queries: list[str] | None = None,
        narrations: list[str] | None = None,
        fmt: str = "story",
    ) -> ProductionPlan:
        visuals = visual_queries or [f"realistic visual {i}" for i in range(len(key_points))]
        texts = narrations or [f"نص القسم {i + 1} بصياغة طبيعية." for i in range(len(key_points))]
        return ProductionPlan(
            topic="كيف تضع حدودًا واضحة دون أن تخسر قربك من الآخرين؟",
            pillar="understand",
            format=fmt,
            hook="أحيانًا لا تكون المشكلة في الرفض، بل في الطريقة التي نفهم بها القرب.",
            title_options=["حدود بلا خصام", "حين يصبح الوضوح احترامًا", "مساحة للعلاقة"],
            thumbnail_concepts=["two chairs", "open doorway", "quiet conversation"],
            sections=[
                ScriptSection(
                    id=f"s{i + 1}",
                    narration=texts[i],
                    visual_query=visuals[i],
                    key_point=key_points[i],
                )
                for i in range(len(key_points))
            ],
            cta="",
            closing_payoff="الوضوح الهادئ قد يحمي مساحة العلاقة بدل أن يهدمها.",
        )

    def _corrected(self) -> ProductionPlan:
        return self._long(
            key_points=[
                "الخوف من الرفض قد يدفع إلى موافقة لا نريدها",
                "الوضوح الهادئ يفرق بين رفض الطلب ورفض الشخص",
                "الحدود المتسقة تمنح العلاقة توقعات أوضح",
            ]
        )

    def test_duplicate_targeting_preserves_first_occurrence(self) -> None:
        plan = self._long(
            key_points=[
                "الفكرة نفسها",
                "الفكرة نفسها!",
                "فكرة مختلفة",
                "الفكرة نفسها",
            ]
        )
        self.assertEqual(
            lifecycle._duplicate_long_key_point_target_ids(plan),
            ["s2", "s4"],
        )

    def test_long_duplicate_key_points_repair_once_then_pass(self) -> None:
        broken = self._long(
            key_points=["الفكرة نفسها", "الفكرة نفسها", "فكرة ثالثة"]
        )
        calls = {"n": 0}

        def repair(plan, issues):
            calls["n"] += 1
            self.assertIs(plan, broken)
            self.assertEqual(issues, ["long_form_duplicate_key_points"])
            return self._corrected()

        resolved = lifecycle.resolve_plan_for_producer_handoff(
            broken,
            research_context={"approved_research_pack": [{"claim": "x"}]},
            long_repair_fn=repair,
        )
        self.assertEqual(calls["n"], 1)
        self.assertEqual(
            resolved.sections[1].key_point,
            "الوضوح الهادئ يفرق بين رفض الطلب ورفض الشخص",
        )

    def test_long_repair_that_still_duplicates_fails_closed_after_one_call(self) -> None:
        broken = self._long(
            key_points=["الفكرة نفسها", "الفكرة نفسها", "فكرة ثالثة"]
        )
        calls = {"n": 0}

        def repair(plan, issues):
            calls["n"] += 1
            return plan

        with self.assertRaisesRegex(
            ProducerQualityContractError,
            "long_form_duplicate_key_points",
        ):
            lifecycle.resolve_plan_for_producer_handoff(
                broken,
                research_context={"approved_research_pack": [{"claim": "x"}]},
                long_repair_fn=repair,
            )
        self.assertEqual(calls["n"], 1)

    def test_unsupported_precise_claim_never_enters_long_auto_repair(self) -> None:
        unsafe = self._long(
            key_points=["فكرة أولى", "فكرة ثانية"],
            narrations=[
                "تشير الدراسات إلى أن 87% من الناس يتغير دماغهم حتمًا بهذه الطريقة.",
                "يمكن ملاحظة اختلاف التجارب بين الناس.",
            ],
        )
        calls = {"n": 0}

        def repair(plan, issues):
            calls["n"] += 1
            return self._corrected()

        with self.assertRaisesRegex(
            ProducerQualityContractError,
            "unsupported_precise_claim_without_approved_research",
        ):
            lifecycle.resolve_plan_for_producer_handoff(
                unsafe,
                research_context={"approved_research_pack": []},
                long_repair_fn=repair,
            )
        self.assertEqual(calls["n"], 0)

    def test_long_visual_field_defect_does_not_use_text_only_dossier_transport(self) -> None:
        broken = self._long(
            key_points=["فكرة أولى", "فكرة ثانية"],
            visual_queries=["realistic desk", ""],
        )
        calls = {"n": 0}

        def repair(plan, issues):
            calls["n"] += 1
            return plan

        with self.assertRaisesRegex(
            ProducerQualityContractError,
            "section_2_visual_query_empty",
        ):
            lifecycle.resolve_plan_for_producer_handoff(
                broken,
                research_context={"approved_research_pack": [{"claim": "x"}]},
                long_repair_fn=repair,
            )
        self.assertEqual(calls["n"], 0)

    def test_long_repair_uses_targeted_run120_transport_and_independent_p1_scope(self) -> None:
        broken = self._long(
            key_points=["الفكرة نفسها", "الفكرة نفسها", "فكرة ثالثة"]
        )
        corrected = self._corrected()
        ledger = BudgetLedger("story", enforce=True)
        parent = SimpleNamespace(
            ledger=ledger,
            requested_model="gemini-2.5-flash",
        )

        with (
            patch.object(lifecycle, "get_active_budget_task", return_value=parent),
            patch.object(
                lifecycle.run120_dossier_repair_hardening,
                "_repair_existing_plan",
                return_value=corrected,
            ) as transport,
        ):
            result = lifecycle._repair_long_plan_once(
                broken,
                ["long_form_duplicate_key_points"],
                args=(
                    "key",
                    broken.topic,
                    "story",
                    "gemini-2.5-flash",
                ),
                kwargs={},
                research_context={"approved_research_pack": [{"claim": "x"}]},
            )

        self.assertIs(result, corrected)
        transport.assert_called_once()
        issue_notes = transport.call_args.args[1]
        self.assertIn('TARGET_SECTION_IDS=["s2"]', issue_notes)
        self.assertIn("Preserve the first occurrence", issue_notes)

        summary = ledger.to_summary()
        self.assertEqual(summary["logical_tasks"]["by_priority"]["P1"], 1)
        self.assertEqual(summary["logical_tasks"]["by_kind"]["OUTLINE_PLAN"], 1)
        self.assertEqual(summary["provider_attempts"]["total"], 0)

        spec = lifecycle._producer_long_repair_spec(1)
        self.assertIs(spec.priority, Priority.P1)
        self.assertEqual(spec.task_id, "PRODUCER_LONG_REPAIR_R1")
        self.assertEqual(spec.max_provider_attempts, 3)

    def test_mixed_repairable_and_unowned_long_issue_fails_closed_without_partial_repair(self) -> None:
        broken = self._long(
            key_points=["الفكرة نفسها", "الفكرة نفسها"],
            visual_queries=["realistic desk", ""],
        )
        calls = {"n": 0}

        def repair(plan, issues):
            calls["n"] += 1
            return self._corrected()

        with self.assertRaises(ProducerQualityContractError):
            lifecycle.resolve_plan_for_producer_handoff(
                broken,
                research_context={"approved_research_pack": [{"claim": "x"}]},
                long_repair_fn=repair,
            )
        self.assertEqual(calls["n"], 0)

    def test_installed_lifecycle_repairs_initial_long_but_never_nests_inside_dossier(self) -> None:
        broken = self._long(
            key_points=["الفكرة نفسها", "الفكرة نفسها", "فكرة ثالثة"]
        )
        corrected = self._corrected()

        def original(api_key, topic, requested_format, content_model, **kwargs):
            return broken

        def detector_only_wrapper(*args, **kwargs):
            raise AssertionError("detector-only wrapper must be replaced")

        detector_only_wrapper._isco_producer_quality_contract = True
        detector_only_wrapper._isco_producer_quality_original = original

        with (
            patch.object(lifecycle, "_INSTALLED", False),
            patch.object(lifecycle.orchestrator, "build_plan", detector_only_wrapper),
            patch.object(
                lifecycle,
                "_active_long_dossier_repair_context",
                return_value=None,
            ),
            patch.object(
                lifecycle,
                "_repair_long_plan_once",
                return_value=corrected,
            ) as repair,
        ):
            lifecycle.install_producer_planning_lifecycle()
            result = lifecycle.orchestrator.build_plan(
                "key",
                broken.topic,
                "story",
                "gemini-2.5-flash",
                research_context={"approved_research_pack": [{"claim": "x"}]},
            )

        self.assertIs(result, corrected)
        repair.assert_called_once()

        with (
            patch.object(lifecycle, "_INSTALLED", False),
            patch.object(lifecycle.orchestrator, "build_plan", detector_only_wrapper),
            patch.object(
                lifecycle,
                "_active_long_dossier_repair_context",
                return_value=(broken, "existing dossier repair"),
            ),
            patch.object(lifecycle, "_repair_long_plan_once") as nested_repair,
        ):
            lifecycle.install_producer_planning_lifecycle()
            with self.assertRaisesRegex(
                ProducerQualityContractError,
                "long_form_duplicate_key_points",
            ):
                lifecycle.orchestrator.build_plan(
                    "key",
                    broken.topic,
                    "story",
                    "gemini-2.5-flash",
                    research_context={"approved_research_pack": [{"claim": "x"}]},
                )

        nested_repair.assert_not_called()


if __name__ == "__main__":
    unittest.main()
