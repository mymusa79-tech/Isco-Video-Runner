from __future__ import annotations

import unittest
from unittest.mock import patch

from isco_video_agent.models import ProductionPlan, ScriptSection

from scripts import producer_planning_lifecycle as lifecycle
from scripts.producer_quality_contract import ProducerQualityContractError


class Run164ProducerShortRepairLifecycleTests(unittest.TestCase):
    def _short(
        self,
        *,
        on_screen: str,
        closing: str,
        hook: str = 'تعتقد أن قول "لا" يجرح مشاعر أحبائك؟',
    ) -> ProductionPlan:
        return ProductionPlan(
            topic='فخ المجاملة المستمرة: لماذا يصعب عليك قول لا حتى لمن تحب؟',
            pillar="understand",
            format="moment",
            hook=hook,
            title_options=[
                "حين تصبح المجاملة عبئًا",
                "حدود بلا خصام",
                "وضوح أقرب",
            ],
            thumbnail_concepts=["door light", "two chairs", "quiet boundary"],
            sections=[
                ScriptSection(
                    id="s1",
                    narration="",
                    visual_query="two friends talking calmly indoors portrait realistic",
                    on_screen_text=on_screen,
                    emotion="reflective",
                    expected_seconds=15.0,
                    key_point="الوضوح الهادئ قد يكون أنفع من موافقة لا تريدها",
                )
            ],
            cta="يمكنك تجربة صياغة ألطف.",
            closing_payoff=closing,
            narrative_format="short_why_reframe",
            editorial_intent={"short_template": "why_reframe"},
        )

    def _corrected(self) -> ProductionPlan:
        return self._short(
            on_screen="لكن الرفض المهذب قد يكون أوضح من موافقة لا تريدها",
            closing="أحيانًا يحفظ الوضوح مساحة العلاقة",
        )

    def test_run164_serialized_on_screen_shape_repairs_once_then_passes(self) -> None:
        broken = self._short(
            on_screen="['المجاملة لا تعني دائمًا الموافقة','حدودك لا تعني الخصام']",
            closing="الاستماع لنفسك يفتح باب الاحترام المتبادل",
        )
        calls = {"n": 0}

        def repair(plan, issues):
            calls["n"] += 1
            self.assertIs(plan, broken)
            self.assertIn("section_1_on_screen_text_serialized_list", issues)
            return self._corrected()

        resolved = lifecycle.resolve_plan_for_producer_handoff(
            broken,
            research_context={"approved_research_pack": []},
            repair_fn=repair,
        )
        self.assertEqual(calls["n"], 1)
        self.assertEqual(
            resolved.sections[0].on_screen_text,
            "لكن الرفض المهذب قد يكون أوضح من موافقة لا تريدها",
        )

    def test_single_repair_that_still_violates_contract_fails_closed(self) -> None:
        broken = self._short(
            on_screen="['السطر الأول','السطر الثاني']",
            closing="الاستماع لنفسك يفتح باب الاحترام المتبادل",
        )
        calls = {"n": 0}

        def repair(plan, issues):
            calls["n"] += 1
            return plan

        with self.assertRaisesRegex(
            ProducerQualityContractError,
            "producer_plan_handoff_blocked",
        ):
            lifecycle.resolve_plan_for_producer_handoff(
                broken,
                research_context={"approved_research_pack": []},
                repair_fn=repair,
            )
        self.assertEqual(calls["n"], 1)

    def test_nonrepairable_factuality_issue_never_enters_short_repair(self) -> None:
        unsafe = self._short(
            on_screen="لكن تشير الدراسات إلى أن 87% من الناس يتغير دماغهم بهذه الطريقة",
            closing="أحيانًا تبدو النتيجة مختلفة",
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
                repair_fn=repair,
            )
        self.assertEqual(calls["n"], 0)

    def test_installed_lifecycle_mirrors_producer_revision_and_repairs_initial_short(self) -> None:
        broken = self._short(
            on_screen="['السطر الأول','السطر الثاني']",
            closing="الاستماع لنفسك يفتح باب الاحترام المتبادل",
        )
        corrected = self._corrected()
        captured = {"calls": 0, "revision_note": ""}

        def original(api_key, topic, requested_format, content_model, **kwargs):
            captured["calls"] += 1
            captured["revision_note"] = kwargs.get("revision_note", "")
            return broken

        def detector_only_wrapper(*args, **kwargs):
            raise AssertionError("detector-only wrapper must be replaced, not called twice")

        detector_only_wrapper._isco_producer_quality_contract = True
        detector_only_wrapper._isco_producer_quality_original = original

        with (
            patch.object(lifecycle, "_INSTALLED", False),
            patch.object(lifecycle.orchestrator, "build_plan", detector_only_wrapper),
            patch.object(
                lifecycle.short_planning_repair,
                "active_short_repair_context",
                return_value=None,
            ),
            patch.object(
                lifecycle,
                "_repair_short_plan_once",
                return_value=corrected,
            ) as repair,
        ):
            lifecycle.install_producer_planning_lifecycle()
            result = lifecycle.orchestrator.build_plan(
                "key",
                broken.topic,
                "moment",
                "gemini-2.5-flash",
                research_context={"approved_research_pack": []},
            )

        self.assertIs(result, corrected)
        self.assertEqual(captured["calls"], 1)
        self.assertIn("Producer pre-gate", captured["revision_note"])
        repair.assert_called_once()

    def test_existing_dossier_repair_context_cannot_nest_a_second_producer_repair(self) -> None:
        broken = self._short(
            on_screen="['السطر الأول','السطر الثاني']",
            closing="الاستماع لنفسك يفتح باب الاحترام المتبادل",
        )

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
                lifecycle.short_planning_repair,
                "active_short_repair_context",
                return_value=(broken, "existing dossier repair"),
            ),
            patch.object(lifecycle, "_repair_short_plan_once") as repair,
        ):
            lifecycle.install_producer_planning_lifecycle()
            with self.assertRaisesRegex(
                ProducerQualityContractError,
                "section_1_on_screen_text_serialized_list",
            ):
                lifecycle.orchestrator.build_plan(
                    "key",
                    broken.topic,
                    "moment",
                    "gemini-2.5-flash",
                    research_context={"approved_research_pack": []},
                )

        repair.assert_not_called()


if __name__ == "__main__":
    unittest.main()
