from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from isco_video_agent.models import ProductionPlan, ScriptSection

from scripts.producer_quality_contract import (
    ProducerQualityContractError,
    certify_producer_handoff,
    plan_quality_issues,
)
from scripts.short_planning_repair import SHORT_REPAIR_PROMPT_MAX_BYTES, build_short_repair_prompt


class ProducerPlanQualityTests(unittest.TestCase):
    def _short(self, *, on_screen: str, closing: str, cta: str = "يمكنك تجربة صياغة ألطف.") -> ProductionPlan:
        return ProductionPlan(
            topic='فخ المجاملة المستمرة: لماذا يصعب عليك قول لا حتى لمن تحب؟',
            pillar="understand",
            format="moment",
            hook='تعتقد أن قول "لا" يجرح مشاعر أحبائك؟',
            title_options=["حين تصبح المجاملة عبئًا", "حدود بلا خصام", "وضوح أقرب"],
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
            cta=cta,
            closing_payoff=closing,
            narrative_format="short_why_reframe",
            editorial_intent={"short_template": "why_reframe"},
        )

    def test_run161_shape_is_blocked_before_independent_tone_audit(self) -> None:
        plan = self._short(
            on_screen="['المجاملة لا تعني دائمًا الموافقة','قل لا بثقة']",
            closing="الاستماع لنفسك يفتح باب الاحترام المتبادل",
        )
        issues = plan_quality_issues(plan, research_context={"approved_research_pack": []})
        self.assertIn("section_1_on_screen_text_serialized_list", issues)
        self.assertIn("why_reframe_missing_explicit_contrast_or_reframe", issues)

    def test_direct_imperative_in_story_beat_is_blocked_but_cta_is_separate(self) -> None:
        story_command = self._short(
            on_screen="قل لا بثقة عندما لا تريد الموافقة",
            closing="أحيانًا يحفظ الوضوح مساحة العلاقة",
        )
        self.assertIn(
            "moment_direct_imperative_in_story_beat",
            plan_quality_issues(story_command, research_context={"approved_research_pack": []}),
        )

        cta_command = self._short(
            on_screen="لكن الرفض المهذب قد يكون أوضح من موافقة لا تريدها",
            closing="أحيانًا يحفظ الوضوح مساحة العلاقة",
            cta="قل لا بلطف حين تحتاج إلى مساحة.",
        )
        self.assertNotIn(
            "moment_direct_imperative_in_story_beat",
            plan_quality_issues(cta_command, research_context={"approved_research_pack": []}),
        )

    def test_corrected_why_reframe_passes_producer_contract(self) -> None:
        plan = self._short(
            on_screen="لكن الرفض المهذب قد يكون أوضح من موافقة لا تريدها",
            closing="أحيانًا يحفظ الوضوح مساحة العلاقة",
        )
        self.assertEqual(
            plan_quality_issues(plan, research_context={"approved_research_pack": []}),
            [],
        )

    def test_empty_research_pack_blocks_precise_study_claim_in_long_form(self) -> None:
        plan = ProductionPlan(
            topic="موضوع",
            pillar="understand",
            format="story",
            hook="سؤال محدد",
            title_options=["أ", "ب", "ج"],
            thumbnail_concepts=["x", "y", "z"],
            sections=[
                ScriptSection(
                    id="s1",
                    narration="تشير الدراسات إلى أن هذه العادة تغيّر النتيجة حتمًا.",
                    visual_query="quiet desk realistic",
                    key_point="فكرة أولى",
                ),
                ScriptSection(
                    id="s2",
                    narration="يمكن ملاحظة أثر العادة في الحياة اليومية دون تعميم.",
                    visual_query="person walking from behind realistic",
                    key_point="فكرة ثانية",
                ),
            ],
            cta="",
            closing_payoff="خلاصة",
        )
        self.assertIn(
            "unsupported_precise_claim_without_approved_research",
            plan_quality_issues(plan, research_context={"approved_research_pack": []}),
        )

    def test_long_form_duplicate_key_points_are_rejected_before_audits(self) -> None:
        plan = ProductionPlan(
            topic="موضوع",
            pillar="understand",
            format="story",
            hook="خطاف",
            title_options=["أ", "ب", "ج"],
            thumbnail_concepts=["x", "y", "z"],
            sections=[
                ScriptSection(id="s1", narration="نص أول", visual_query="desk light", key_point="الفكرة نفسها"),
                ScriptSection(id="s2", narration="نص ثان", visual_query="window light", key_point="الفكرة نفسها"),
            ],
            cta="",
            closing_payoff="خلاصة",
        )
        self.assertIn(
            "long_form_duplicate_key_points",
            plan_quality_issues(plan, research_context={"approved_research_pack": [{"claim": "x"}]}),
        )

    def test_compact_short_repair_carries_selected_template_contract(self) -> None:
        plan = self._short(
            on_screen="المجاملة لا تعني دائمًا الموافقة",
            closing="الاستماع لنفسك يفتح باب الاحترام المتبادل",
        )
        prompt = build_short_repair_prompt(
            plan,
            "Tone audit found preachy wording and missing template progression.",
            research_context={"approved_research_pack": []},
        )
        self.assertIn("Selected Short template: why_reframe", prompt)
        self.assertIn("why_reframe must visibly progress", prompt)
        self.assertIn("Producer pre-gate", prompt)
        self.assertLessEqual(len(prompt.encode("utf-8")), SHORT_REPAIR_PROMPT_MAX_BYTES)


class ProducerMediaHandoffTests(unittest.TestCase):
    def _write_json(self, root: Path, name: str, value) -> None:
        (root / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def _long_fixture(self, root: Path, *, visual_reviewed: int = 2) -> None:
        plan = {
            "format": "film",
            "sections": [
                {"id": "s1", "narration": "أ", "visual_query": "desk", "key_point": "واحد"},
                {"id": "s2", "narration": "ب", "visual_query": "window", "key_point": "اثنان"},
            ],
        }
        quality = {
            "format": "film",
            "duration_ok": True,
            "audio_ok": True,
            "av_sync_ok": True,
            "video_streams": 1,
            "audio_streams": 1,
            "visual_sections_reviewed": visual_reviewed,
        }
        rights = {"visuals": [{"provider": "pexels", "asset_id": 1}]}
        self._write_json(root, "plan.json", plan)
        self._write_json(root, "quality-final.json", quality)
        self._write_json(root, "rights-manifest.json", rights)
        self._write_json(root, "monetization-check.json", {"status": "PASS_WITH_UPLOAD_ACTIONS"})
        self._write_json(root, "visual-audit.json", [{"section": "s1"}, {"section": "s2"}])
        self._write_json(root, "audio-mastering.json", {"status": "applied"})
        (root / "final.mp4").write_bytes(b"0" * 2048)

    def _finished_short_fixture(self, root: Path, *, voice_rights: bool = True) -> None:
        self._write_json(
            root,
            "plan.json",
            {
                "format": "moment",
                "sections": [
                    {"id": "s1", "narration": "", "visual_query": "quiet room portrait", "key_point": "زاوية"}
                ],
            },
        )
        self._write_json(
            root,
            "quality-final.json",
            {
                "format": "moment",
                "duration_ok": True,
                "audio_ok": True,
                "av_sync_ok": True,
                "video_streams": 1,
                "audio_streams": 1,
                "visual_sections_reviewed": 1,
                "short_voice_v2_refresh": True,
            },
        )
        rights = {
            "visuals": [{"provider": "pexels", "asset_id": 22}],
            "short_cinematic_v1": {"recorded": True},
        }
        if voice_rights:
            rights["short_voice_v2"] = {"generated": True, "provider": "gemini"}
        self._write_json(root, "rights-manifest.json", rights)
        self._write_json(root, "monetization-check.json", {"status": "PASS_WITH_UPLOAD_ACTIONS"})
        self._write_json(root, "visual-audit.json", [{"section": "s1"}])
        self._write_json(
            root,
            "short-intelligence-pre-gold.json",
            {"stage": "pre_gold", "compensation": {"beat_driven_multi_shot_applied": True}},
        )
        self._write_json(root, "short-visual-timeline.json", {"shot_count": 3, "distinct_asset_count": 3})
        (root / "final.mp4").write_bytes(b"short-final-v2" * 256)

    def test_long_audio_video_rights_handoff_passes_before_final_qc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._long_fixture(root)
            receipt = certify_producer_handoff(root)
            self.assertEqual(receipt["decision"], "pass")
            self.assertTrue(receipt["checks"]["long_audio_mastering_applied"])
            self.assertTrue(receipt["checks"]["visual_section_coverage"])
            self.assertEqual(receipt["extra_ai_calls"], 0)
            report = json.loads((root / "producer-handoff-quality.json").read_text(encoding="utf-8"))
            self.assertEqual(report["decision"], "pass")

    def test_finished_short_handoff_binds_voice_cinematic_and_exact_final_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._finished_short_fixture(root)
            receipt = certify_producer_handoff(root)
            expected_sha = hashlib.sha256((root / "final.mp4").read_bytes()).hexdigest()
            self.assertEqual(receipt["phase"], "short_finished")
            self.assertEqual(receipt["final_sha256"], expected_sha)
            self.assertTrue(receipt["checks"]["short_voice_quality_refresh"])
            self.assertTrue(receipt["checks"]["short_voice_rights"])
            self.assertTrue(receipt["checks"]["short_multi_shot_present"])
            self.assertTrue(receipt["checks"]["short_final_assets_distinct"])
            self.assertTrue(receipt["checks"]["short_cinematic_rights"])
            self.assertEqual(receipt["extra_ai_calls"], 0)

    def test_finished_short_missing_voice_provenance_cannot_reach_final_qc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._finished_short_fixture(root, voice_rights=False)
            with self.assertRaisesRegex(ProducerQualityContractError, "short_voice_rights"):
                certify_producer_handoff(root)

    def test_incomplete_visual_stage_cannot_be_handed_to_final_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._long_fixture(root, visual_reviewed=1)
            with self.assertRaisesRegex(ProducerQualityContractError, "visual_section_coverage"):
                certify_producer_handoff(root)


if __name__ == "__main__":
    unittest.main()
