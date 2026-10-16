from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2 import owner_pack
from clean_v2.contracts import compute_brief_sha256
from clean_v2.identity_sequence import PRAYER_SENTENCE
from clean_v2.pipeline import CleanV2Pipeline
from scripts.test_clean_v2 import (
    _FakeVisuals,
    _FakeVoice,
    _infrastructure_visual_qa,
    _passing_audio_mastering,
    _passing_cinematic_layer,
    _passing_final_master_qc,
    _passing_text_audit,
)

MESSAGE = """شورت جاهز
عنوان: لماذا تتعب وأنت لم تفعل شيئًا؟
نص 1: تنهي يومك منهكًا ولا تعرف ماذا أنجزت. السبب ليس الكسل بل كثرة القرارات الصغيرة التي استهلكت انتباهك قبل أن تبدأ عملك الحقيقي.
نص 2: كل رسالة تفتحها وكل خيار تؤجله يأخذ جزءًا من طاقتك. حين تتراكم هذه الأشياء الصغيرة يصير التعب حقيقيًا حتى لو بدا يومك فارغًا.
نص 3: اختر أهم عمل واحد قبل أن تفتح هاتفك. بعد ذلك تصبح بقية اليوم أخف لأن عقلك عرف أين يضع انتباهه الأول.
مشهد 1: man staring at crowded phone notifications at night
مشهد 2: desk covered with unfinished sticky notes in dim light
مشهد 3: hand closing laptop lid with a tired sigh
مشهد 4: person scrolling through endless messages on a sofa
مشهد 5: open calendar page with many crossed out tasks
مشهد 6: hand writing one single task on a clean notebook page
مشهد 7: person placing phone in a drawer and starting to work
"""


def _brief() -> dict:
    return {
        "approved_by_user": True,
        "approved_topic": "لماذا تتعب وأنت لم تفعل شيئًا؟",
        "format": "short",
        "language": "ar",
        "audience": "Arabic-speaking adults",
        "editorial_intent": "نص كتبه المالك.",
        "research_pack": [],
        "hard_constraints": ["No fabricated facts."],
    }


class ParseTests(unittest.TestCase):
    def test_complete_message_parses(self) -> None:
        pack = owner_pack.parse_owner_message(MESSAGE)
        self.assertEqual(len(pack["texts"]), 3)
        self.assertEqual(len(pack["scenes"]), 7)
        self.assertTrue(pack["title"].startswith("لماذا"))

    def test_arabic_digits_and_missing_fields_are_explained(self) -> None:
        converted = MESSAGE.replace("نص 2:", "نص ٢:").replace("مشهد 4:", "مشهد ٤:")
        self.assertEqual(owner_pack.parse_owner_message(converted)["scenes"][3][:6], "person")
        with self.assertRaisesRegex(owner_pack.OwnerPackError, "مشهد 7"):
            owner_pack.parse_owner_message(MESSAGE.split("مشهد 7")[0])
        with self.assertRaisesRegex(owner_pack.OwnerPackError, "نص 3"):
            owner_pack.parse_owner_message(MESSAGE.split("نص 3")[0])

    def test_flattened_message_still_parses(self) -> None:
        flat = " ".join(MESSAGE.split())
        self.assertEqual(owner_pack.parse_owner_message(flat), owner_pack.parse_owner_message(MESSAGE))

    def test_scenes_are_optional_and_ordinal_labels_work(self) -> None:
        body = MESSAGE.split("مشهد 1")[0].replace("نص 1:", "النص الأول:").replace("نص 2:", "النص الثاني:")
        pack = owner_pack.parse_owner_message(body)
        self.assertEqual(pack["scenes"], list(owner_pack.DEFAULT_SCENES))
        self.assertEqual(len(pack["texts"]), 3)

    def test_rejects_bad_scenes_and_lengths(self) -> None:
        with self.assertRaisesRegex(owner_pack.OwnerPackError, "بالإنجليزية"):
            owner_pack.parse_owner_message(MESSAGE.replace("man staring at crowded phone notifications at night", "رجل ينظر إلى الهاتف"))
        with self.assertRaisesRegex(owner_pack.OwnerPackError, "مكررة"):
            owner_pack.parse_owner_message(MESSAGE.replace("person scrolling through endless messages on a sofa", "desk covered with unfinished sticky notes in dim light"))
        with self.assertRaisesRegex(owner_pack.OwnerPackError, "قصير"):
            owner_pack.parse_owner_message(
                "شورت جاهز\nعنوان: x\nنص 1: جملة.\nنص 2: جملة.\nنص 3: جملة.\n"
                + "\n".join(f"مشهد {i}: some english scene words {i}" for i in range(1, 8))
            )


class BuildTests(unittest.TestCase):
    def test_artifacts_carry_identity_and_seven_beats(self) -> None:
        pack = owner_pack.parse_owner_message(MESSAGE)
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            script = owner_pack.build_artifacts(pack, _brief(), out)
            first = script["sections"][0]["narration"]
            self.assertIn(PRAYER_SENTENCE, first)
            self.assertNotIn("هنا نداء اليقظة", first)
            self.assertTrue(first.startswith("تنهي يومك منهكًا"))
            story = json.loads((out / "visual-story.json").read_text(encoding="utf-8"))
            self.assertEqual([b["role"] for b in story["beats"]],
                             ["hook", "hook", "hook", "body", "body", "body", "payoff"])
            self.assertEqual(
                [b["section_id"] for b in story["beats"]],
                ["s1", "s1", "s1", "s2", "s2", "s3", "s3"],
            )


class PipelineTests(unittest.TestCase):
    def test_owner_text_reaches_voice_without_any_model_call(self) -> None:
        class _ForbiddenRouter:
            events: list[dict] = []

            def route(self, **_kwargs):
                raise AssertionError("no model may write or audit owner text")

        def _forbidden_audit(**_kwargs):
            raise AssertionError("owner text is not re-audited by a model")

        pack = owner_pack.parse_owner_message(MESSAGE)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack_path = root / "pack.json"
            pack_path.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
            brief_path = root / "brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            voice = _FakeVoice()
            output = root / "out"
            with mock.patch.dict(os.environ, {owner_pack.OWNER_PACK_ENV: str(pack_path)}):
                with self.assertRaisesRegex(RuntimeError, "CLEAN_V2_VISUAL_QA_INFRASTRUCTURE"):
                    CleanV2Pipeline(
                        router=_ForbiddenRouter(),
                        voice_synthesizer=voice,
                        visual_source=_FakeVisuals(),
                        visual_qa=_infrastructure_visual_qa,
                        cinematic_layer=_passing_cinematic_layer,
                        final_master_qc=_passing_final_master_qc,
                        text_audit=_forbidden_audit,
                        audio_mastering=_passing_audio_mastering,
                        narrative_identity=lambda **_k: self.fail("identity model"),
                    ).run(
                        brief_path=brief_path,
                        approved_sha256=compute_brief_sha256(brief),
                        output_dir=output,
                        engine_sha="a" * 40,
                        runner_sha="b" * 40,
                        max_visuals=7,
                    )
            self.assertGreaterEqual(voice.calls, 1)
            manifest = json.loads((output / "run-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["owner_script"]["provider_calls"], 0)
            spoken = (output / "narration.txt").read_text(encoding="utf-8")
            self.assertIn("تنهي يومك منهكًا", spoken)
            self.assertIn("اختر أهم عمل واحد", spoken)


if __name__ == "__main__":
    unittest.main()
