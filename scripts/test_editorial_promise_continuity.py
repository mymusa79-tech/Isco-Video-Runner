from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from isco_video_agent.editorial_room import intent_from_dict

from scripts import editorial_promise_continuity as continuity
from scripts import orchestration_shorts_port
from scripts import shorts_production_binding as short_core


RUN198_TOPIC = "جلد الذات بعد كل لقاء: لماذا تراجع كل كلمة قلتها؟"


def _passing_engine_evidence() -> dict:
    return {
        "schema_version": 1,
        "decision": "pass",
        "flags": [],
        "semantic_authority": "engine_tone_quality_same_provider_call",
        "provider_calls_added": 0,
        "repair_owner": "existing_tone_repair_dossier",
        "validation": "valid",
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

    def test_engine_continuity_evidence_is_the_only_semantic_delivery_authority(self) -> None:
        evidence = _passing_engine_evidence()
        self.assertEqual(continuity._engine_continuity_evidence({"editorial_promise_continuity": evidence}), evidence)

    def test_delivery_rejects_missing_or_nonpassing_engine_semantic_evidence(self) -> None:
        with self.assertRaisesRegex(
            continuity.EditorialPromiseContinuityError,
            "short_delivery_engine_continuity_evidence_missing",
        ):
            continuity._engine_continuity_evidence({})

        blocked = _passing_engine_evidence()
        blocked["decision"] = "block"
        blocked["flags"] = ["editorial_promise_continuity: adjacent answer"]
        with self.assertRaisesRegex(
            continuity.EditorialPromiseContinuityError,
            "short_delivery_engine_continuity_not_passed",
        ):
            continuity._engine_continuity_evidence({"editorial_promise_continuity": blocked})

    def test_delivery_rejects_fabricated_numeric_or_wrong_authority_evidence(self) -> None:
        fabricated = _passing_engine_evidence()
        fabricated["semantic_authority"] = "final_critic_numeric_proxy"
        with self.assertRaisesRegex(
            continuity.EditorialPromiseContinuityError,
            "short_delivery_engine_continuity_authority_invalid",
        ):
            continuity._engine_continuity_evidence({"editorial_promise_continuity": fabricated})

    def test_install_does_not_monkeypatch_core_short_finalizer(self) -> None:
        original = short_core.finalize_short_quality
        continuity.install_editorial_promise_continuity()
        self.assertIs(short_core.finalize_short_quality, original)

    def test_explicit_orchestration_port_calls_core_and_certifier_once(self) -> None:
        core_report = {"quality": "core-pass"}
        certified_report = {"quality": "core-pass", "editorial_promise_continuity": {"decision": "pass"}}
        with (
            patch.object(orchestration_shorts_port.core, "finalize_short_quality", return_value=core_report) as core_finalize,
            patch.object(continuity, "certify_short_delivery", return_value=certified_report) as certify,
        ):
            result = orchestration_shorts_port.finalize_short_quality(
                Path("/tmp/short"),
                {"approved_topic": RUN198_TOPIC},
                {"pre_gold": True},
            )
        self.assertIs(result, certified_report)
        core_finalize.assert_called_once()
        certify.assert_called_once_with(
            Path("/tmp/short"),
            {"approved_topic": RUN198_TOPIC},
            core_report,
        )

    def test_certifier_writes_engine_authority_and_keeps_numeric_score_as_craft_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "plan.json").write_text(
                json.dumps(
                    {
                        "topic": RUN198_TOPIC,
                        "cta": "بعد اللقاء سمِّ الجملة التي ما زلت تعيدها",
                        "closing_payoff": "بعد اللقاء افصل بين ما حدث وبين تفسيرك له",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (root / "tone-quality-audit.json").write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "editorial_promise_continuity": _passing_engine_evidence(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            report = {"promise_payoff_score": 88.0}
            control = {
                "approved_topic": RUN198_TOPIC,
                "short_admission": {
                    "single_action_contract": "بعد اللقاء سمِّ جملة واحدة ما زلت تعيدها"
                },
            }
            result = continuity.certify_short_delivery(root, control, report)
            self.assertIs(result, report)
            self.assertEqual(
                result["editorial_promise_continuity"]["semantic_authority"],
                "engine_tone_quality_same_provider_call",
            )
            self.assertEqual(
                result["evidence_provenance"]["promise_payoff_numeric_score_role"],
                "existing_final_critic_craft_proxy_not_semantic_authority",
            )
            self.assertFalse(result["evidence_provenance"]["semantic_promise_score_fabricated"])
            persisted = json.loads((root / "short-intelligence.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["editorial_promise_continuity"]["decision"], "pass")


if __name__ == "__main__":
    unittest.main()
