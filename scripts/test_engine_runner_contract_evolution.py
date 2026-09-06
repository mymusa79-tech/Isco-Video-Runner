from __future__ import annotations

import copy
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import isco_video_agent.resilient_planner as staged
from isco_video_agent.models import ProductionPlan, ScriptSection

from scripts import planning_outline_split_contract as split
from scripts import planning_stage_contract as stage_contract


def _raw_intent() -> dict:
    return {
        "editorial_thesis": "التغيير يبدأ بفهم الحلقة التي تسبق التشتت اليومي",
        "viewer_starting_belief": "أظن أن التركيز يعتمد فقط على قوة الإرادة الشخصية",
        "hidden_assumption": "أتعامل مع البيئة كأنها لا تؤثر فعليًا في انتباهي",
        "editorial_turn": "تصميم البيئة قد يسبق محاولة إجبار النفس على التركيز",
        "stakes": "الفهم الخاطئ يبدد الجهد اليومي دون نتيجة ثابتة أو واضحة",
        "viewer_promise": "ستتعلم كيف تختبر محفزات التشتت حولك بطريقة عملية",
        "evidence_boundaries": ["لا ندعي تشخيصًا طبيًا أو علاجًا سريريًا لهذا السلوك"],
        "earned_payoff": "ابدأ بتغيير محفز واحد ثم راقب أثره بوضوح خلال يومك",
    }


def _raw_core() -> dict:
    narrative = next(iter(staged._NARRATIVE_FORMATS))
    return {
        "pillar": "understand",
        "hook": "ربما لا تبدأ مشكلة التركيز من إرادتك أصلًا",
        "title_options": ["العنوان الأول", "العنوان الثاني", "العنوان الثالث"],
        "thumbnail_concepts": ["مكتب مشتت", "هاتف بعيد", "مساحة هادئة"],
        "cta": "جرّب تغيير محفز واحد اليوم وراقب الفرق",
        "closing_payoff": "غيّر البيئة قبل أن تلوم إرادتك مرة أخرى",
        "narrative_format": narrative,
        "opener_variant": "لنبدأ من الشيء الذي يحدث قبل التشتت",
        "closer_variant": "راقب ما تغيّر عندما تغيّر ما حولك",
        "transition_variants": ["لكن هنا تبدأ المفارقة", "وهذا يقودنا للسؤال التالي", "الآن تظهر الخطوة العملية"],
        "editorial_intent": _raw_intent(),
    }


def _sections(count: int) -> list[dict]:
    return [
        {
            "id": f"s{index}",
            "purpose": f"فكرة مستقلة رقم {index} تخدم الحجة",
            "visual_query": f"quiet workspace detail {index}",
            "on_screen_text": f"خطوة {index}",
            "emotion": "reflective",
            "expected_seconds": 30,
        }
        for index in range(1, count + 1)
    ]


def _canonical_outline() -> dict:
    data = _raw_core()
    data["editorial_intent"] = staged.intent_from_dict(data["editorial_intent"]).to_dict()
    data["section_briefs"] = _sections(staged._SECTION_COUNTS["film"])
    return data


class EngineRunnerContractEvolutionTests(unittest.TestCase):
    def test_provider_transport_rejects_host_owned_metadata(self) -> None:
        payload = _raw_core()
        payload["editorial_intent"] = dict(payload["editorial_intent"])
        payload["editorial_intent"]["editorial_fingerprint"] = "0" * 24
        payload["editorial_intent"]["persona_version"] = 1
        spec = split.outline_core_stage_spec(staged._SECTION_COUNTS["film"])
        contract = stage_contract.bind_request_contract(spec, "transport-core")

        with self.assertRaises(stage_contract.PlanningStageError) as caught:
            split._validate_core(payload, contract)
        self.assertEqual(caught.exception.code, stage_contract.PlanningErrorCode.STRUCTURAL_INVALID)
        self.assertIn("unexpected=editorial_fingerprint,persona_version", str(caught.exception))

    def test_canonical_outline_accepts_engine_metadata_and_rejects_stale_fingerprint(self) -> None:
        expected = staged._SECTION_COUNTS["film"]
        data = _canonical_outline()
        full = stage_contract.outline_stage_spec(expected)
        contract = stage_contract.bind_request_contract(full, "canonical-outline")

        with (
            mock.patch.object(staged, "validate_narrative_format", return_value=[]),
            mock.patch.object(staged, "validate_identity_phrases", return_value=[]),
        ):
            returned = split._validate_canonical_outline(data, contract, expected)
        self.assertEqual(returned, json.loads(json.dumps(data, ensure_ascii=False)))

        stale = copy.deepcopy(data)
        stale["editorial_intent"]["editorial_fingerprint"] = "f" * 24
        with (
            mock.patch.object(staged, "validate_narrative_format", return_value=[]),
            mock.patch.object(staged, "validate_identity_phrases", return_value=[]),
            self.assertRaises(stage_contract.PlanningStageError) as caught,
        ):
            split._validate_canonical_outline(stale, contract, expected)
        self.assertEqual(caught.exception.code, stage_contract.PlanningErrorCode.SEMANTIC_INVALID)
        self.assertIn("host_fingerprint_mismatch", str(caught.exception))

    def test_locked_premise_sizing_is_identical_before_and_after_engine_enrichment(self) -> None:
        raw = _raw_core()
        canonical = copy.deepcopy(raw)
        canonical["editorial_intent"] = staged.intent_from_dict(raw["editorial_intent"]).to_dict()
        self.assertEqual(
            split.locked_premise_utf8_bytes(raw),
            split.locked_premise_utf8_bytes(canonical),
        )

    def test_preflight_fixture_contains_same_post_enrichment_metadata_shape(self) -> None:
        from scripts import planning_envelope_preflight as preflight

        premise = preflight._bounded_preflight_locked_premise()
        intent = premise["editorial_intent"]
        self.assertEqual(len(intent["editorial_fingerprint"]), 24)
        self.assertIsInstance(intent["persona_version"], int)
        measured = split.locked_premise_utf8_bytes(premise)
        self.assertLessEqual(measured, split.LOCKED_PREMISE_MAX_UTF8_BYTES)
        self.assertGreaterEqual(measured, int(split.LOCKED_PREMISE_MAX_UTF8_BYTES * 0.90))

    def test_real_pinned_engine_outline_canonicalizes_between_the_two_calls(self) -> None:
        engine_outline = inspect.unwrap(staged._outline)
        calls: list[str] = []
        raw = _raw_core()
        sections = {"section_briefs": _sections(staged._SECTION_COUNTS["film"])}

        def fake_json(_api_key, prompt, model="model"):
            calls.append(prompt)
            return copy.deepcopy(raw if len(calls) == 1 else sections)

        with (
            mock.patch.object(staged, "json_text", side_effect=fake_json),
            mock.patch.object(staged, "validate_narrative_format", return_value=[]),
            mock.patch.object(staged, "validate_identity_phrases", return_value=[]),
        ):
            result = engine_outline(
                "key",
                topic="كيف تستعيد تركيزك؟",
                fmt="film",
                model="model",
                policy_json="{}",
                research_json="{}",
                avoid_json="{}",
                learning_json="{}",
                revision_note="",
            )

        self.assertEqual(len(calls), 2)
        self.assertIn("editorial_fingerprint", calls[1])
        self.assertIn("persona_version", calls[1])
        self.assertEqual(set(result["editorial_intent"]), set(split._CANONICAL_INTENT_FIELDS))

        expected = staged._SECTION_COUNTS["film"]
        contract = stage_contract.bind_request_contract(
            stage_contract.outline_stage_spec(expected),
            "real-engine-canonical-outline",
        )
        with (
            mock.patch.object(staged, "validate_narrative_format", return_value=[]),
            mock.patch.object(staged, "validate_identity_phrases", return_value=[]),
        ):
            split._validate_canonical_outline(result, contract, expected)

    def test_engine_runner_model_field_matrix_is_exact(self) -> None:
        observed = split.assert_engine_runner_contract_compatible()
        self.assertEqual(observed["EditorialIntent"], split._CANONICAL_INTENT_FIELDS)
        self.assertEqual(observed["ProductionPlan"], split._EXPECTED_PRODUCTION_PLAN_FIELDS)
        self.assertEqual(observed["ScriptSection"], split._EXPECTED_SCRIPT_SECTION_FIELDS)

    def test_plan_json_must_equal_production_plan_projection(self) -> None:
        canonical_intent = staged.intent_from_dict(_raw_intent()).to_dict()
        plan = ProductionPlan(
            topic="موضوع الاختبار",
            pillar="understand",
            format="film",
            hook="مقدمة الاختبار",
            title_options=["أ", "ب", "ج"],
            thumbnail_concepts=["1", "2", "3"],
            sections=[
                ScriptSection(
                    id="s1",
                    narration="نص صالح للاختبار",
                    visual_query="quiet room",
                    on_screen_text="اختبار",
                    emotion="reflective",
                    expected_seconds=20.0,
                    key_point="فكرة مستقلة",
                )
            ],
            cta="جرّب الآن",
            closing_payoff="الخلاصة",
            identity_opener="افتتاحية",
            identity_closer="خاتمة",
            identity_transitions=["انتقال 1", "انتقال 2", "انتقال 3"],
            narrative_format=next(iter(staged._NARRATIVE_FORMATS)),
            editorial_intent=canonical_intent,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "plan.json"
            path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False), encoding="utf-8")
            split._assert_exact_plan_json_projection(root, plan)

            poisoned = json.loads(path.read_text(encoding="utf-8"))
            poisoned["identity_closer"] = "خاتمة مختلفة"
            path.write_text(json.dumps(poisoned, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(stage_contract.PlanningStageError) as caught:
                split._assert_exact_plan_json_projection(root, plan)
            self.assertEqual(caught.exception.code, stage_contract.PlanningErrorCode.STRUCTURAL_INVALID)
            self.assertIn("identity_closer", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
