from __future__ import annotations

import unittest

from isco_video_agent.editorial_room import intent_from_dict

from scripts import editorial_promise_continuity as continuity
from scripts import native_short_planner_router as short_router
from scripts import shorts_production_binding as short_core


RUN198_TOPIC = "جلد الذات بعد كل لقاء: لماذا تراجع كل كلمة قلتها؟"


def _engine_evidence(*, decision: str = "pass", flags: list[str] | None = None, validation: str = "valid") -> dict:
    return {
        "schema_version": 1,
        "decision": decision,
        "flags": list(flags or []),
        "semantic_authority": "engine_tone_quality_same_provider_call",
        "provider_calls_added": 0,
        "repair_owner": "existing_tone_repair_dossier",
        "validation": validation,
    }


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

    def test_standalone_short_gets_valid_full_editorial_intent_and_locked_contract(self) -> None:
        prior = {
            "short_template": "why_reframe",
            "short_template_selection": {"selected": "why_reframe"},
            "short_compensation_v2": {"beat_shape": ["hook_misbelief", "contrast", "reframe", "payoff_action"]},
        }
        result = continuity._standalone_editorial_intent(RUN198_TOPIC, "why_reframe", prior)
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
        evidence = continuity._source_action_alignment(
            {
                "approval_scope": "short_sibling",
                "source_semantic_job": first_job,
                "short_admission": {"single_action_contract": first},
            }
        )
        self.assertTrue(evidence["pass"])
        self.assertEqual(evidence["reason"], "source_job_anchor_present")

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

    def test_standalone_does_not_require_sibling_single_action_contract(self) -> None:
        evidence = continuity._source_action_alignment(
            {
                "approval_scope": "short_only",
                "short_admission": {},
            }
        )
        self.assertTrue(evidence["pass"])
        self.assertEqual(
            evidence["reason"],
            "standalone_or_non_sibling_no_source_action_contract_required",
        )

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

    def test_engine_capability_evidence_accepts_pass_and_semantic_block_for_existing_repair_owner(self) -> None:
        passing = _engine_evidence()
        self.assertIs(
            continuity.require_engine_continuity_evidence(
                {"editorial_promise_continuity": passing}
            ),
            passing,
        )
        blocked = _engine_evidence(
            decision="block",
            flags=["editorial_promise_continuity: adjacent answer"],
        )
        self.assertIs(
            continuity.require_engine_continuity_evidence(
                {"editorial_promise_continuity": blocked}
            ),
            blocked,
        )

    def test_engine_capability_evidence_rejects_missing_or_fabricated_authority(self) -> None:
        with self.assertRaisesRegex(
            continuity.EditorialPromiseContinuityError,
            "engine_continuity_evidence_missing",
        ):
            continuity.require_engine_continuity_evidence({})

        fabricated = _engine_evidence()
        fabricated["semantic_authority"] = "final_critic_numeric_proxy"
        with self.assertRaisesRegex(
            continuity.EditorialPromiseContinuityError,
            "engine_continuity_authority_invalid",
        ):
            continuity.require_engine_continuity_evidence(
                {"editorial_promise_continuity": fabricated}
            )

    def test_engine_technical_block_remains_fail_closed_without_stealing_repair_semantics(self) -> None:
        technical = _engine_evidence(decision="block", validation="malformed")
        self.assertIs(
            continuity.require_engine_continuity_evidence(
                {"editorial_promise_continuity": technical}
            ),
            technical,
        )

    def test_install_does_not_modify_certified_short_core_or_prompt_revision_function(self) -> None:
        original_finalizer = short_core.finalize_short_quality
        original_revision = short_router.merge_short_template_revision
        continuity.install_editorial_promise_continuity()
        self.assertIs(short_core.finalize_short_quality, original_finalizer)
        self.assertIs(short_router.merge_short_template_revision, original_revision)


if __name__ == "__main__":
    unittest.main()
