from __future__ import annotations

import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2.contextual_cta import ContextualCtaError, bind_contextual_cta_to_script
from clean_v2.contracts import compute_brief_sha256
from clean_v2.pipeline import (
    CTA_BIND_STAGE,
    CleanV2Pipeline,
    _tone_repair_prompt,
    _validate_and_apply_script_patches,
)
from clean_v2.short_format import ShortFormatError


# Run #77 attempt 2: Planning's exact CTA appeared in the writer's s3
# anchor, while the host chose s2. Text below preserves that failure pattern
# from the archived writer anchors, not an unavailable full script dump.
CTA = "اشارك في التعليق: ما هو أول خطوة صغيرة ستجربها لتوقف هذا الدوار؟"


def _plan() -> dict:
    return {
        "title": "خارج النص",
        "cta": CTA,
        "sections": [{"id": f"s{i}"} for i in range(1, 4)],
    }


def _podcast_script() -> dict:
    return {
        "sections": [
            {"id": "s1", "narration": "A: لماذا أشعر أن كل جهدي لا يكفي؟ B: أنت تقيس يومك بما تبقى معلقًا. الكوب يبدو دائمًا نصف فارغ."},
            {"id": "s2", "narration": "A: هل أتوقف عن السعي؟ B: المشكلة في المقياس. القائمة الطويلة تبرز الفجوة. التقدم الصغير غائب عن هذا المقياس."},
            {"id": "s3", "narration": f"A: كيف أخرج من الحلقة؟ B: معيار نجاح يومك مهمة واحدة صغيرة. {CTA} يصبح تقدمك مرئيًا."},
        ],
    }


class Run77CtaClosureTests(unittest.TestCase):
    def test_writer_cta_elsewhere_is_rebound_once_for_film_and_podcast(self) -> None:
        for fmt in ("film", "podcast"):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as tmp:
                script = _podcast_script()
                if fmt == "film":
                    for section in script["sections"]:
                        section["narration"] = re.sub(r"[AB]:\s*", "", section["narration"])
                untouched_opening = script["sections"][0]["narration"]
                report = bind_contextual_cta_to_script(
                    output_dir=Path(tmp), brief={"format": fmt}, plan=_plan(), script=script,
                )
                self.assertEqual(report["anchor_section_id"], "s2")
                self.assertEqual(report["existing_cta_copies_removed"], 1)
                self.assertEqual(report["provider_calls_added"], 0)
                self.assertEqual(script["sections"][0]["narration"], untouched_opening)
                self.assertEqual(" ".join(s["narration"] for s in script["sections"]).count(CTA), 1)
                self.assertIn(CTA, script["sections"][1]["narration"])
                self.assertNotIn(CTA, script["sections"][2]["narration"])
                if fmt == "podcast":
                    before = script["sections"][1]["narration"].split(CTA)[0]
                    self.assertEqual(re.findall(r"([AB]):\s+", before)[-1], "B")
                first = copy.deepcopy(script)
                bind_contextual_cta_to_script(
                    output_dir=Path(tmp), brief={"format": fmt}, plan=_plan(), script=script,
                )
                self.assertEqual(script, first)

    def test_copies_in_opening_and_a_turn_are_removed_without_losing_topic_or_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = _podcast_script()
            script["sections"][0]["narration"] = f"A: لماذا لا يكفي جهدي؟ {CTA} B: المقياس يغفل تقدمك."
            script["sections"][1]["narration"] += " A: وهل أستطيع تغيير المقياس؟"
            report = bind_contextual_cta_to_script(
                output_dir=Path(tmp), brief={"format": "podcast"}, plan=_plan(), script=script,
            )
            self.assertEqual(report["existing_cta_copies_removed"], 2)
            self.assertNotIn(CTA, script["sections"][0]["narration"])
            anchor = script["sections"][1]["narration"]
            self.assertEqual(re.findall(r"([AB]):\s+", anchor), ["A", "B", "A"])
            self.assertIn("التقدم الصغير غائب عن هذا المقياس. A:", anchor)
            self.assertEqual(anchor.count(CTA), 1)

    def test_authored_cta_without_host_terminal_period_is_not_duplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            authored = "اكتب تجربتك في التعليقات"
            script = _podcast_script()
            script["sections"][2]["narration"] = f"A: ما الخطوة؟ B: معيار واحد يكفي. {authored} ويصبح التقدم مرئيًا."
            report = bind_contextual_cta_to_script(
                output_dir=Path(tmp), brief={"format": "podcast"},
                plan={**_plan(), "cta": authored}, script=script,
            )
            joined = " ".join(s["narration"] for s in script["sections"])
            self.assertEqual(joined.count(authored), 1)
            self.assertEqual(report["spoken_text"], authored + ".")

    def test_missing_b_turn_fails_closed_and_short_remains_unchanged(self) -> None:
        script = _podcast_script()
        script["sections"][1]["narration"] = "A: هل يكفي مقياس مختلف؟"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ContextualCtaError, "longform_cta_must_appear_exactly_once"):
                bind_contextual_cta_to_script(
                    output_dir=Path(tmp), brief={"format": "podcast"}, plan=_plan(), script=script,
                )
            short = {"sections": [{"id": "s1", "narration": "لماذا نتردد؟"}, {"id": "s2", "narration": "المقارنات تزداد."}, {"id": "s3", "narration": "القائمة الأقصر أوضح. حدّد ثلاثة خيارات فقط قبل اتخاذ القرار"}]}
            before = copy.deepcopy(short)
            report = bind_contextual_cta_to_script(
                output_dir=Path(tmp), brief={"format": "short"}, plan=_plan(), script=short,
            )
            self.assertEqual(short, before)
            self.assertEqual(report["mode"], "none")
            self.assertEqual(report["spoken_text"], "")

    def test_runtime_binding_failure_records_stage_and_reason_before_voice(self) -> None:
        from scripts.test_clean_v2 import _FakeRouter, _brief, _passing_narrative_identity

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path = root / "brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            voice = mock.Mock()
            visuals = mock.Mock(events=[])
            pipeline = CleanV2Pipeline(
                router=_FakeRouter(), voice_synthesizer=voice, visual_source=visuals,
                narrative_identity=_passing_narrative_identity,
            )
            code = "longform_cta_must_appear_exactly_once"
            with mock.patch("clean_v2.contextual_cta.bind_contextual_cta_to_script", side_effect=ContextualCtaError(code)):
                with self.assertRaisesRegex(ContextualCtaError, code):
                    pipeline.run(brief_path=brief_path, approved_sha256=compute_brief_sha256(brief),
                                 output_dir=root / "output", engine_sha="a" * 40, runner_sha="b" * 40)
            manifest = json.loads((root / "output/run-manifest.json").read_text())
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["failure_classification"], "pre-layer")
            self.assertEqual(manifest["failure_origin_stage"], CTA_BIND_STAGE)
            self.assertEqual(manifest["failure_reason"], code)
            self.assertEqual(manifest["stages"][-1]["error_code"], code)
            self.assertEqual(manifest["stages"][-1]["status"], "failed")
            checkpoint = json.loads((root / "output/resume-checkpoint.json").read_text())
            self.assertEqual(checkpoint["completed_stage"], "script")
            voice.synthesize.assert_not_called()
            visuals.acquire.assert_not_called()


class Run76ShortPayoffRepairTests(unittest.TestCase):
    ACTION = "حدّد ثلاثة خيارات فقط قبل اتخاذ القرار"
    PAYOFF = "عندما تتقلص قائمة خياراتك."

    def setUp(self) -> None:
        self.plan = {"title": "كثرة الخيارات", "practical_action_ar": self.ACTION,
                     "s3_locked_action": self.ACTION, "sections": [{"id": f"s{i}"} for i in range(1, 4)]}
        self.script = {"title": "كثرة الخيارات", "sections": [
            {"id": "s1", "narration": "لماذا يصعب القرار حين تكثر الخيارات؟"},
            {"id": "s2", "narration": "المقارنات الكثيرة تجعل كل بديل منافسًا جديدًا."},
            {"id": "s3", "s3_payoff": self.PAYOFF, "s3_locked_action": self.ACTION,
             "narration": self.PAYOFF + " " + self.ACTION},
        ]}

    def _patch(self, find: str, replace: str) -> dict:
        return _validate_and_apply_script_patches(
            {"patches": [{"section_id": "s3", "find": find, "replace": replace}]},
            plan=self.plan, original_script=self.script, identity={}, cta_plan={},
            revision_note="s3: dangling fragment 'عندما تتقلص قائمة خياراتك.'", is_short_format=True,
        )

    def test_complete_payoff_patch_preserves_exact_one_locked_action(self) -> None:
        replacement = "عندما تتقلص قائمة خياراتك، تقل المقارنات."
        repaired = self._patch(self.PAYOFF, replacement)
        closing = repaired["sections"][2]
        self.assertEqual(closing["s3_payoff"], replacement)
        self.assertEqual(closing["s3_locked_action"], self.ACTION)
        self.assertEqual(closing["narration"], replacement + " " + self.ACTION)
        self.assertEqual(closing["narration"].count(self.ACTION), 1)
        self.assertEqual(repaired["sections"][:2], self.script["sections"][:2])

    def test_second_action_and_locked_action_edits_stay_rejected(self) -> None:
        with self.assertRaisesRegex(ShortFormatError, "short_s3_payoff_contains_forbidden_action_family"):
            self._patch(self.PAYOFF, "اختر قائمة أقصر لتقل المقارنات.")
        with self.assertRaises(ValueError):
            self._patch(self.ACTION, "حدّد خيارين فقط قبل اتخاذ القرار")
        self.assertEqual(self.script["sections"][2]["s3_locked_action"], self.ACTION)

    def test_short_prompt_requires_complete_descriptive_payoff_without_action_rewrite(self) -> None:
        prompt = _tone_repair_prompt(
            brief={"format": "short", "research_pack": []}, plan=self.plan, script=self.script,
            identity={}, cta_plan={}, revision_note="s3: dangling fragment",
        )
        self.assertIn("dangling subordinate clause", prompt)
        self.assertIn("do not turn it into advice or add a second action", prompt)
        self.assertIn("never include it in patch.find or patch.replace", prompt)
        self.assertIn("host appends it exactly once", prompt)


if __name__ == "__main__":
    unittest.main()
