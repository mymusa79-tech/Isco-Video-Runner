from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from clean_v2 import owner_art, owner_pack, owner_podcast
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
)

QUESTIONS = [
    "لماذا نشعر بالتعب رغم أننا لم نفعل شيئًا؟",
    "وما علاقة القرارات الصغيرة بذلك؟",
    "كيف أعرف أني أعتمد على نفسي أكثر من اللازم؟",
    "وهل هذا خطأ دائمًا؟",
    "ماذا أفعل في الصباح؟",
    "ومتى أطلب المساعدة؟",
    "وما الخطوة الأولى؟",
]


def dialogue(pairs: int = 14, answer_words: int = 60) -> str:
    out = []
    for index in range(pairs):
        answer = f"هذه إجابة رقم {index} مفصلة بما يكفي. " + " ".join(["كلمة"] * answer_words) + "."
        out.append(f"A: {QUESTIONS[index % len(QUESTIONS)]}\nB: {answer}")
    return "\n".join(out)


MESSAGE = "بودكاست جاهز\nعنوان: عبء الاعتماد على الذات: هل استقلاليتك حيلة دفاعية؟\nغلاف: استقلالية أم دفاع\n" + dialogue()


def _brief(title: str) -> dict:
    return {
        "approved_by_user": True,
        "approved_topic": title,
        "format": "podcast",
        "language": "ar",
        "audience": "Arabic-speaking adults",
        "editorial_intent": "حوار كتبه المالك.",
        "research_pack": [],
        "hard_constraints": ["No fabricated facts."],
    }


class ParseTests(unittest.TestCase):
    def test_complete_message_parses(self) -> None:
        pack = owner_podcast.parse_podcast_message(MESSAGE)
        self.assertEqual(pack["format"], "podcast")
        self.assertEqual(len(pack["turns"]), 28)
        self.assertEqual(pack["cover_text"], "استقلالية أم دفاع")
        self.assertEqual(pack["cta"], owner_podcast.DEFAULT_CTA)

    def test_flattened_message_parses(self) -> None:
        pack = owner_podcast.parse_podcast_message(" ".join(MESSAGE.split()))
        self.assertEqual(len(pack["turns"]), 28)

    def test_dialogue_rules_are_explained_in_arabic(self) -> None:
        bad_first = MESSAGE.replace("A: لماذا نشعر", "B: لماذا نشعر", 1)
        with self.assertRaisesRegex(owner_pack.OwnerPackError, "A"):
            owner_podcast.parse_podcast_message(bad_first)
        no_title = MESSAGE.replace("عنوان:", "موضوع:", 1)
        with self.assertRaisesRegex(owner_pack.OwnerPackError, "عنوان"):
            owner_podcast.parse_podcast_message(no_title)
        with self.assertRaisesRegex(owner_pack.OwnerPackError, "قصير"):
            owner_podcast.parse_podcast_message("بودكاست جاهز\nعنوان: x\nA: سؤال؟\nB: جواب.")
        long_questions = MESSAGE.replace("وهل هذا خطأ دائمًا؟", "وهل هذا خطأ دائمًا " + "كلمة " * 30 + "؟")
        with self.assertRaisesRegex(owner_pack.OwnerPackError, "أطول من 20"):
            owner_podcast.parse_podcast_message(long_questions)

    def test_bad_cta_is_rejected_and_good_one_kept(self) -> None:
        with self.assertRaisesRegex(owner_pack.OwnerPackError, "الدعوة"):
            owner_podcast.parse_podcast_message(MESSAGE.replace("غلاف:", "دعوة: مرحبا بكم\nغلاف:", 1))
        pack = owner_podcast.parse_podcast_message(MESSAGE.replace("غلاف:", "دعوة: اكتب رأيك في التعليقات.\nغلاف:", 1))
        self.assertEqual(pack["cta"], "اكتب رأيك في التعليقات.")

    def test_sections_split_on_pairs_and_stay_2_to_5(self) -> None:
        pack = owner_podcast.parse_podcast_message(MESSAGE)
        groups = owner_podcast.section_groups(pack["turns"])
        self.assertTrue(2 <= len(groups) <= 5)
        for group in groups:
            self.assertEqual(group[0][0], "A")
            self.assertEqual(group[-1][0], "B")
        self.assertEqual(sum(len(g) for g in groups), 28)


class ArtifactTests(unittest.TestCase):
    def test_artifacts_carry_prayer_cta_and_valid_story(self) -> None:
        pack = owner_podcast.parse_podcast_message(MESSAGE)
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            script = owner_pack.build_artifacts(pack, _brief(pack["title"]), out)
            joined = " ".join(s["narration"] for s in script["sections"])
            self.assertIn(PRAYER_SENTENCE, joined)
            self.assertIn("شاركنا رأيك في التعليقات", joined)
            self.assertTrue(script["sections"][0]["narration"].startswith("A: لماذا نشعر"))
            plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["narrative_format"], "dialogue_qa")
            story = json.loads((out / "visual-story.json").read_text(encoding="utf-8"))
            self.assertEqual(len(story["beats"]), len(plan["sections"]))


class DraftTests(unittest.TestCase):
    def test_parts_sort_by_message_id_and_images_classify(self) -> None:
        draft = owner_podcast.new_draft()
        owner_podcast.add_part(draft, 12, "B: ثانيًا")
        owner_podcast.add_part(draft, 10, "بودكاست جاهز\nA: أولًا؟")
        self.assertTrue(owner_podcast.assemble(draft).index("أولًا") < owner_podcast.assemble(draft).index("ثانيًا"))
        self.assertTrue(owner_podcast.is_follow_up("تكملة\nA: x"))
        self.assertTrue(owner_podcast.is_follow_up("تكملة: A: x"))
        self.assertFalse(owner_podcast.is_follow_up("تكملتها"))
        self.assertEqual(owner_podcast.follow_up_body("تكملة\nA: x"), "A: x")

        def img(i, caption=""):
            return {"message_id": i, "file_id": f"f{i}", "unique_id": f"u{i}", "caption": caption, "kind": "document"}

        one = owner_podcast.classify_art([img(1)])
        self.assertEqual((len(one["body"]), one["cover"]["unique_id"]), (1, "u1"))
        two = owner_podcast.classify_art([img(2), img(1)])
        self.assertEqual((two["body"][0]["unique_id"], two["cover"]["unique_id"]), ("u1", "u2"))
        three = owner_podcast.classify_art([img(1), img(2), img(3)])
        self.assertEqual((len(three["body"]), three["cover"]), (3, None))
        marked = owner_podcast.classify_art([img(1, "غلاف"), img(2), img(3)])
        self.assertEqual((marked["cover"]["unique_id"], len(marked["body"])), ("u1", 2))
        message = {"message_id": 5, "photo": [{"file_id": "s", "file_size": 10}, {"file_id": "b", "file_size": 99, "file_unique_id": "ub"}]}
        self.assertEqual(owner_podcast.image_from_message(message)["file_id"], "b")
        self.assertIsNone(owner_podcast.image_from_message({"document": {"mime_type": "application/pdf", "file_id": "x"}}))


class ArtTests(unittest.TestCase):
    def test_normalize_and_motion_clip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Image.new("RGB", (1000, 1300), (30, 40, 90)).save(root / "raw.png")
            normalized = owner_art.normalize_image(root / "raw.png", root / "image-1.jpg")
            self.assertEqual(Image.open(normalized).size, (1920, 1080))
            clip = owner_art.make_motion_clip(normalized, root / "c.mp4", seconds=2.5, direction=1)
            from clean_v2.media import probe_duration

            self.assertAlmostEqual(probe_duration(clip), 2.5, delta=0.2)

    def test_source_only_takes_over_podcast_with_images(self) -> None:
        class Inner:
            def acquire(self, *args, **kwargs):
                return ["stock"], [{"provider": "pexels"}]

        plan = {"sections": [{"id": "s1"}], "visual_story": {"beats": [{"id": "b1", "section_id": "s1"}]}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            art = root / "art"
            art.mkdir()
            source = owner_art.OwnerArtVisualSource(Inner(), art)
            self.assertEqual(source.acquire(plan, root / "o", "podcast", 5)[0], ["stock"])  # no images yet
            Image.new("RGB", (1920, 1080), (10, 10, 40)).save(art / "image-1.jpg")
            self.assertEqual(source.acquire(plan, root / "o", "short", 5)[0], ["stock"])  # other formats untouched
            clips, rights = source.acquire(plan, root / "o", "podcast", 5, {"s1": 1.0})
            self.assertEqual(len(clips), 1)
            self.assertEqual(rights[0]["provider"], "owner_art")
            self.assertEqual(rights[0]["section_id"], "s1")
            self.assertTrue(clips[0].is_file())

    def test_fetch_downloads_normalises_and_names_files(self) -> None:
        from scripts import fetch_owner_art

        def fake(_token, file_id, destination):
            Image.new("RGB", (800, 800), (50, 50, 50)).save(destination, "PNG")
            return destination

        art = {"body": [{"file_id": "a"}, {"file_id": "b"}], "cover": {"file_id": "c"}}
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "art"
            written = fetch_owner_art.fetch(art, out, "token", fetcher=fake)
            self.assertEqual(written, ["image-1.jpg", "image-2.jpg", "cover.jpg"])
            self.assertEqual(Image.open(out / "cover.jpg").size, owner_art.COVER_SIZE)
            self.assertEqual([p.name for p in owner_art.body_images(out)], ["image-1.jpg", "image-2.jpg"])


class PipelineTests(unittest.TestCase):
    def test_owner_dialogue_reaches_voice_without_any_model_call(self) -> None:
        class _ForbiddenRouter:
            events: list[dict] = []

            def route(self, **_kwargs):
                raise AssertionError("no model may write or audit owner text")

        def _forbidden_audit(**_kwargs):
            raise AssertionError("owner text is not re-audited by a model")

        pack = owner_podcast.parse_podcast_message(MESSAGE)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack_path = root / "pack.json"
            pack_path.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
            brief = _brief(pack["title"])
            brief_path = root / "brief.json"
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
                        max_visuals=5,
                    )
            self.assertGreaterEqual(voice.calls, 1)
            manifest = json.loads((output / "run-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["owner_script"]["provider_calls"], 0)
            spoken = (output / "narration.txt").read_text(encoding="utf-8")
            self.assertIn("هذه إجابة رقم 0", spoken)
            self.assertIn(PRAYER_SENTENCE, spoken)
            self.assertIn("شاركنا رأيك في التعليقات", spoken)
            cta = json.loads((output / "cta-plan.json").read_text(encoding="utf-8"))
            self.assertEqual(cta["mode"], "comment")


if __name__ == "__main__":
    unittest.main()
