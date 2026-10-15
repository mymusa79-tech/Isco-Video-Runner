from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from clean_v2 import owner_art, owner_film, owner_pack
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

FILLER = " ".join(["كلمة"] * 70)


def message(sections: int = 5, extra: str = "") -> str:
    texts = "\n".join(
        f"نص {i}: هذه الجملة الأولى للفصل {i}. وهذه جملة ثانية تشرح الفكرة. وهذه ثالثة تختم الفقرة. {FILLER}."
        for i in range(1, sections + 1)
    )
    return f"فيلم جاهز\nعنوان: لماذا نتعب ونحن لم نفعل شيئًا؟\n{extra}{texts}"


def _brief(title: str) -> dict:
    return {
        "approved_by_user": True,
        "approved_topic": title,
        "format": "film",
        "language": "ar",
        "audience": "Arabic-speaking adults",
        "editorial_intent": "نص كتبه المالك.",
        "research_pack": [],
        "hard_constraints": ["No fabricated facts."],
    }


class ParseTests(unittest.TestCase):
    def test_complete_message_and_flattened_message_parse(self) -> None:
        for text in (message(), " ".join(message().split())):
            pack = owner_film.parse_film_message(text)
            self.assertEqual((pack["format"], len(pack["texts"])), ("film", 5))
            self.assertEqual(pack["cta"], owner_film.DEFAULT_CTA)

    def test_errors_are_explained_in_arabic(self) -> None:
        with self.assertRaisesRegex(owner_pack.OwnerPackError, "نص 5"):
            owner_film.parse_film_message(message(4))
        with self.assertRaisesRegex(owner_pack.OwnerPackError, "قصير"):
            owner_film.parse_film_message("فيلم جاهز\nعنوان: x\n" + "\n".join(f"نص {i}: جملة قصيرة." for i in range(1, 6)))
        with self.assertRaisesRegex(owner_pack.OwnerPackError, "A:"):
            owner_film.parse_film_message(message().replace("نص 2: ", "نص 2: A: سؤال؟ B: جواب. ", 1))
        with self.assertRaisesRegex(owner_pack.OwnerPackError, "الدعوة"):
            owner_film.parse_film_message(message(extra="دعوة: مرحبا بكم\n"))
        pack = owner_film.parse_film_message(message(extra="دعوة: اكتب رأيك في التعليقات.\n"))
        self.assertEqual(pack["cta"], "اكتب رأيك في التعليقات.")


class TypeLineTests(unittest.TestCase):
    def test_type_line_is_validated_and_reaches_the_plan(self) -> None:
        pack = owner_film.parse_film_message(message(extra="نوع: paradox\n"))
        self.assertEqual(pack["type"], "paradox")
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            owner_pack.build_artifacts(pack, _brief(pack["title"]), out)
            plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["narrative_format"], "paradox")
        self.assertNotIn("type", owner_film.parse_film_message(message()))
        with self.assertRaisesRegex(owner_pack.OwnerPackError, "النوع غير مدعوم"):
            owner_film.parse_film_message(message(extra="نوع: dialogue_qa\n"))  # podcast-only
        with self.assertRaisesRegex(owner_pack.OwnerPackError, "النوع غير مدعوم"):
            owner_film.parse_film_message(message(extra="نوع: mystery\n"))

    def test_the_word_inside_body_text_is_not_a_label(self) -> None:
        text = message().replace("نص 2: ", "نص 2: هناك نوع: واحد فقط من التعب. ", 1)
        pack = owner_film.parse_film_message(text)
        self.assertNotIn("type", pack)
        self.assertIn("نوع: واحد", pack["texts"][1])

    def test_short_type_line_and_voice_style(self) -> None:
        from clean_v2 import pipeline

        base = (
            "شورت جاهز\nعنوان: لماذا نؤجل ما نريده؟\n"
            + "نوع: Why-Reframe\n"
            + "نص 1: هل تؤجل كل شيء؟ هذه الجملة الثانية للنص. " + FILLER[:60] + ".\n"
            + "نص 2: " + FILLER[:80] + ".\nنص 3: " + FILLER[:80] + "."
        )
        pack = owner_pack.parse_owner_message(base)
        self.assertEqual(pack["type"], "why_reframe")
        with self.assertRaisesRegex(owner_pack.OwnerPackError, "النوع غير مدعوم"):
            owner_pack.parse_owner_message(base.replace("Why-Reframe", "paradox"))
        self.assertEqual(
            pipeline._voice_performance_mode_for_brief(
                {"format": "short", "approved_topic": "قصة ذات يوم"},
                {"owner_authored": True, "short_template": "why_reframe"},
            ),
            "why_reframe",
        )


class ArtifactTests(unittest.TestCase):
    def test_artifacts_keep_film_character(self) -> None:
        pack = owner_film.parse_film_message(message())
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            script = owner_pack.build_artifacts(pack, _brief(pack["title"]), out)
            first = script["sections"][0]["narration"]
            self.assertTrue(first.startswith("هذه الجملة الأولى للفصل 1."))
            self.assertIn(PRAYER_SENTENCE, first)
            self.assertNotIn("A:", " ".join(s["narration"] for s in script["sections"]))
            joined = " ".join(s["narration"] for s in script["sections"])
            self.assertIn("شاركنا رأيك في التعليقات", joined)
            plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual((plan["narrative_format"], len(plan["sections"])), ("direct_cinematic", 5))
            story = json.loads((out / "visual-story.json").read_text(encoding="utf-8"))
            self.assertEqual(len(story["beats"]), 15)
            self.assertEqual(story["beats"][0]["role"], "hook")


class ArtTests(unittest.TestCase):
    def test_film_beats_cut_between_images_with_different_moves(self) -> None:
        plan = {
            "sections": [{"id": "s1"}],
            "visual_story": {"beats": [{"id": "b1", "section_id": "s1"}]},
        }

        class Inner:
            def acquire(self, *a, **k):
                raise AssertionError("film with owner images must not search stock")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            art = root / "art"
            art.mkdir()
            for index, color in enumerate(((20, 30, 80), (80, 30, 20), (20, 80, 30)), start=1):
                Image.new("RGB", (1920, 1080), color).save(art / f"image-{index}.jpg")
            source = owner_art.OwnerArtVisualSource(Inner(), art)
            clips, rights = source.acquire(plan, root / "o", "film", 5, {"s1": 4.0})
            from clean_v2.media import probe_duration

            self.assertEqual((len(clips), rights[0]["provider"]), (1, "owner_art"))
            self.assertGreater(probe_duration(clips[0]), 8.0)  # 4s section + padding, split in segments
            self.assertEqual({owner_art.make_motion_clip.__name__}, {"make_motion_clip"})

    def test_four_distinct_film_moves(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Image.new("RGB", (1920, 1080), (40, 40, 40)).save(root / "i.jpg")
            seen = set()
            with mock.patch("clean_v2.media._run") as run:
                for direction in range(4):
                    owner_art.make_motion_clip(root / "i.jpg", root / "c.mp4", seconds=3, direction=direction, film=True)
                    seen.add(run.call_args[0][0][run.call_args[0][0].index("-vf") + 1])
            self.assertEqual(len(seen), 4)


class PacingTests(unittest.TestCase):
    def test_shot_lengths_vary_sum_exactly_and_never_flash(self) -> None:
        plan = owner_art.segment_plan(40.0, 0)
        self.assertAlmostEqual(sum(plan), 40.0, places=3)
        self.assertGreater(len(set(round(x, 2) for x in plan)), 2)
        self.assertGreaterEqual(min(plan), 4.0)
        self.assertEqual(owner_art.segment_plan(5.0, 0), [5.0])  # a short idea stays one shot
        self.assertGreater(max(owner_art.segment_plan(60.0, 3, 1.25)), max(owner_art.segment_plan(60.0, 3, 0.8)))
        self.assertGreater(owner_art.film_pace("paradox")[0], owner_art.film_pace("connected_list")[0])
        self.assertEqual(owner_art.film_pace(None), (1.0, 1.0))

    def test_sequence_dissolves_into_one_clip_of_the_requested_length(self) -> None:
        from clean_v2.media import probe_duration

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            images = []
            for index, color in enumerate(((20, 30, 80), (80, 30, 20), (20, 80, 30))):
                Image.new("RGB", (1920, 1080), color).save(root / f"i{index}.jpg")
                images.append(root / f"i{index}.jpg")
            clip, used = owner_art.make_sequence_clip(images, root / "c.mp4", seconds=17.0, start_index=0)
            self.assertGreaterEqual(used, 2)
            self.assertAlmostEqual(probe_duration(clip), 17.0, delta=0.4)


class PipelineTests(unittest.TestCase):
    def test_owner_film_reaches_voice_without_any_model_call(self) -> None:
        class _ForbiddenRouter:
            events: list[dict] = []

            def route(self, **_kwargs):
                raise AssertionError("no model may write or audit owner text")

        pack = owner_film.parse_film_message(message())
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
                        text_audit=lambda **_k: self.fail("owner text is not re-audited"),
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
            self.assertIn("هذه الجملة الأولى للفصل 3", (output / "narration.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
