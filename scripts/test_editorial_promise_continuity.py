from __future__ import annotations

import unittest
from types import SimpleNamespace

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.tone_quality as engine_tone
from isco_video_agent.editorial_room import intent_from_dict

from scripts import editorial_promise_continuity as continuity


RUN198_TOPIC = "جلد الذات بعد كل لقاء: لماذا تراجع كل كلمة قلتها؟"


class EditorialPromiseContinuityTests(unittest.TestCase):
    def test_run198_after_to_before_drift_is_detected(self) -> None:
        self.assertEqual(
            continuity._temporal_conflict(
                RUN198_TOPIC,
                "جرب التوقف لحظة قبل الرد حتى تصبح كلماتك أقوى",
            ),
            "approved_after_context_shifted_to_before_action",
        )
        self.assertIsNone(
            continuity._temporal_conflict(
                RUN198_TOPIC,
                "بعد اللقاء اكتب الجملة التي ما زلت تعيدها واسأل هل لديك دليل أنها كانت خطأ",
            )
        )

    def test_reverse_time_frame_drift_is_detected_without_blocking_same_frame(self) -> None:
        self.assertEqual(
            continuity._temporal_conflict(
                "قبل المقابلة: لماذا تتوقع الأسوأ؟",
                "بعد المقابلة راجع كل ما حدث",
            ),
            "approved_before_context_shifted_to_after_action",
        )
        self.assertIsNone(
            continuity._temporal_conflict(
                "قبل المقابلة: لماذا تتوقع الأسوأ؟",
                "قبل المقابلة سمِّ التوقع الذي يضغط عليك",
            )
        )

    def test_prompt_extension_is_idempotent_and_uses_existing_tone_schema(self) -> None:
        plan = SimpleNamespace(
            topic=RUN198_TOPIC,
            format="moment",
            hook="هل تراجع كل كلمة بعد اللقاء؟",
            cta="",
            closing_payoff="بعد اللقاء افصل بين ما حدث وما تتخيله عنه",
            sections=[
                SimpleNamespace(
                    id="s1",
                    key_point="مراجعة الكلام بعد اللقاء لا تعني تلقائيًا أنك أخطأت",
                    on_screen_text="أنت تبحث عن يقين بعد موقف انتهى",
                    visual_query="person alone after a social meeting reflective realistic",
                    emotion="reflective",
                )
            ],
            editorial_intent={
                "short_promise_contract": {
                    "approved_topic": RUN198_TOPIC,
                    "template": "why_reframe",
                }
            },
        )
        first = continuity._append_continuity_contract("BASE PROMPT", plan)
        second = continuity._append_continuity_contract(first, plan)
        self.assertEqual(first, second)
        self.assertEqual(first.count("[EDITORIAL_PROMISE_CONTINUITY_V1]"), 1)
        self.assertIn("AFTER a meeting", first)
        self.assertIn("BEFORE replying", first)
        self.assertIn("visual_query", first)
        self.assertIn("narrative_format_flags", first)
        self.assertIn("editorial_promise_continuity:", first)
        self.assertIn(RUN198_TOPIC, first)

    def test_standalone_short_gets_valid_full_editorial_intent_and_locked_short_contract(self) -> None:
        prior = {
            "short_template": "why_reframe",
            "short_template_selection": {"selected": "why_reframe"},
            "short_compensation_v2": {"beat_shape": ["hook_misbelief", "contrast", "reframe", "payoff_action"]},
        }
        result = continuity._standalone_editorial_intent(
            RUN198_TOPIC,
            "why_reframe",
            prior,
        )
        canonical = intent_from_dict(result).to_dict()
        self.assertEqual(canonical["viewer_promise"], result["viewer_promise"])
        self.assertEqual(result["short_template"], "why_reframe")
        self.assertEqual(result["short_template_selection"], prior["short_template_selection"])
        contract = result["short_promise_contract"]
        self.assertEqual(contract["approved_topic"], RUN198_TOPIC)
        self.assertEqual(contract["template"], "why_reframe")
        self.assertTrue(contract["same_problem_required"])
        self.assertFalse(contract["adjacent_advice_allowed"])
        self.assertEqual(contract["extra_ai_calls"], 0)
        self.assertIn("after", contract["time_context_frame"])

    def test_sibling_single_action_is_local_to_each_semantic_job(self) -> None:
        first_job = "مراجعة كلامك بعد اللقاء لا تعني أنك أخطأت"
        second_job = "الخوف من حكم الآخرين يضخم تفسير الصمت"
        first = continuity._localized_single_action(first_job, "understand")
        second = continuity._localized_single_action(second_job, "understand")
        self.assertNotEqual(first, second)
        self.assertIn(first_job, first)
        self.assertIn(second_job, second)

        first_evidence = continuity._source_action_alignment(
            {
                "approval_scope": "short_sibling",
                "source_semantic_job": first_job,
                "short_admission": {"single_action_contract": first},
            }
        )
        self.assertTrue(first_evidence["pass"])
        self.assertEqual(first_evidence["reason"], "source_job_anchor_present")

    def test_generic_sibling_action_without_source_job_anchor_fails_closed(self) -> None:
        evidence = continuity._source_action_alignment(
            {
                "approval_scope": "short_sibling",
                "source_semantic_job": "مراجعة كلامك بعد اللقاء لا تعني أنك أخطأت",
                "short_admission": {
                    "single_action_contract": "لاحظ موقفًا واحدًا اليوم واسأل ما الذي يحرّكه فعلًا"
                },
            }
        )
        self.assertFalse(evidence["pass"])
        self.assertEqual(evidence["reason"], "source_job_anchor_missing")

    def test_source_short_local_intent_is_valid_and_keeps_parent_evidence_boundary(self) -> None:
        request = {
            "source_semantic_job": "مراجعة كلامك بعد اللقاء لا تعني أنك أخطأت",
            "source_long_topic": RUN198_TOPIC,
            "source_episode_excerpt": {
                "source_section_id": "s4",
                "source_narration_sha256": "a" * 64,
                "source_narration": (
                    "بعد اللقاء قد تبدأ في إعادة كل جملة قلتها. "
                    "المشكلة أن الذاكرة لا تعطيك يقينًا عمّا فكر فيه الآخرون. "
                    "الفائدة أن تفصل بين ما حدث فعلًا وما أضفته أنت بعد انتهاء الموقف."
                ),
            },
            "source_editorial_intent": {
                "stakes": "الاجترار يستهلك الانتباه بعد الموقف بدل أن يضيف معلومة جديدة.",
                "evidence_boundaries": ["لا نشخّص اضطرابًا نفسيًا."],
            },
        }
        result = continuity._source_local_editorial_intent(request)
        canonical = intent_from_dict(result).to_dict()
        self.assertEqual(canonical["viewer_promise"], result["viewer_promise"])
        self.assertEqual(result["source_scope"]["kind"], "source_section_local")
        self.assertEqual(result["source_scope"]["source_section_id"], "s4")
        self.assertIn("لا نشخّص اضطرابًا نفسيًا.", result["evidence_boundaries"])
        self.assertTrue(result["source_scope"]["parent_editorial_intent_preserved_separately"])

    def test_tone_adapter_adds_no_provider_call_and_restores_provider_surface(self) -> None:
        original_audit = orchestrator.audit_tone_and_naturalness
        original_json_text = engine_tone.json_text
        original_openrouter = engine_tone.openrouter
        calls: list[str] = []

        def fake_json_text(key: str, prompt: str, *, model: str):
            calls.append(prompt)
            return {"status": "pass"}

        def fake_audit(api_key: str, plan: object, model: str):
            engine_tone.json_text(api_key, "ORIGINAL TONE PROMPT", model=model)
            return {"status": "pass", "narrative_format_flags": []}

        try:
            orchestrator.audit_tone_and_naturalness = fake_audit
            engine_tone.json_text = fake_json_text
            continuity._install_tone_continuity_adapter()
            plan = SimpleNamespace(
                topic=RUN198_TOPIC,
                format="moment",
                hook="بعد اللقاء لماذا تعيد كل كلمة؟",
                cta="",
                closing_payoff="بعد اللقاء افصل بين الحدث وتفسيرك",
                sections=[],
                editorial_intent={},
            )
            result = orchestrator.audit_tone_and_naturalness("key", plan, "model")
            self.assertEqual(len(calls), 1)
            self.assertIn("[EDITORIAL_PROMISE_CONTINUITY_V1]", calls[0])
            self.assertEqual(result["editorial_promise_continuity"]["provider_calls_added"], 0)
            self.assertEqual(result["editorial_promise_continuity"]["decision"], "pass")
            self.assertIs(engine_tone.json_text, fake_json_text)
            self.assertIs(engine_tone.openrouter, original_openrouter)
        finally:
            orchestrator.audit_tone_and_naturalness = original_audit
            engine_tone.json_text = original_json_text
            engine_tone.openrouter = original_openrouter

    def test_tone_adapter_restores_provider_surface_when_underlying_audit_raises(self) -> None:
        original_audit = orchestrator.audit_tone_and_naturalness
        original_json_text = engine_tone.json_text
        original_openrouter = engine_tone.openrouter

        def exploding_audit(api_key: str, plan: object, model: str):
            raise RuntimeError("boom")

        try:
            orchestrator.audit_tone_and_naturalness = exploding_audit
            continuity._install_tone_continuity_adapter()
            plan = SimpleNamespace(
                topic=RUN198_TOPIC,
                format="moment",
                hook="",
                cta="",
                closing_payoff="",
                sections=[],
                editorial_intent={},
            )
            with self.assertRaisesRegex(RuntimeError, "boom"):
                orchestrator.audit_tone_and_naturalness("key", plan, "model")
            self.assertIs(engine_tone.json_text, original_json_text)
            self.assertIs(engine_tone.openrouter, original_openrouter)
        finally:
            orchestrator.audit_tone_and_naturalness = original_audit
            engine_tone.json_text = original_json_text
            engine_tone.openrouter = original_openrouter


if __name__ == "__main__":
    unittest.main()
