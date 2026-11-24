"""Story film: production.json template "story" is a narrowed film (one narrator, generated stills)."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from clean_v2 import owner_pack, production_package as package
from clean_v2.identity_sequence import LONG_CHANNEL_DEFINITION, PRAYER_SENTENCE, STORY_CHANNEL_DEFINITION
from scripts.test_production_package import archive_bytes, imported_pack, manifest


def _sentence(number: int) -> str:
    return f"في اليوم {number} حدث موقف صغير مع أحمد وتغيّر شيء بسيط في يومه العادي."


def story_manifest(words_per_section: int = 17, scene_plan=(1, 2, 1, 1, 2)) -> dict:
    """Five sections of numbered unique sentences (about 85 words with the default) and 7 stills."""
    doc = manifest("film")
    doc["template"] = "story"
    counter = 0
    sections, scenes = [], []
    for index, take in enumerate(scene_plan, start=1):
        sentences = []
        for _ in range(words_per_section // 17 * 1 or 1):
            counter += 1
            sentences.append(_sentence(counter))
        # Make the section long enough: repeat unique sentences until it reaches the target.
        while len(" ".join(sentences).split()) < words_per_section * 5:
            counter += 1
            sentences.append(_sentence(counter))
        sections.append({"id": f"s{index}", "text": " ".join(sentences)})
        for pick in range(take):
            scenes.append({
                "id": f"image_{len(scenes) + 1}", "section_id": f"s{index}",
                "asset": f"assets/scene_{len(scenes) + 1:02d}.png",
                "start_text": sentences[0 if pick == 0 else len(sentences) // 2],
                "motion": "slow_zoom" if len(scenes) % 2 else "slow_pan", "transition": "dissolve",
            })
    doc["script"] = {"sections": sections}
    doc["scenes"] = scenes
    return doc


class StoryImportTests(unittest.TestCase):
    def test_story_film_is_accepted(self):
        doc = story_manifest()
        words = len(package.spoken_text(" ".join(package.section_text(s) for s in doc["script"]["sections"])).split())
        self.assertTrue(350 <= words <= 550, words)
        checked = package.inspect_archive(archive_bytes(doc))
        self.assertEqual(checked["manifest"]["template"], "story")
        self.assertEqual(checked["manifest"]["format"], "film")

    def test_story_rejects_stock_scenes(self):
        doc = story_manifest()
        doc["scenes"][1]["stock"] = {"url": "https://www.pexels.com/video/hand-typing-on-phone-1234567/"}
        with self.assertRaises(owner_pack.OwnerPackError):
            package.inspect_archive(archive_bytes(doc))

    def test_story_rejects_too_few_or_too_many_scenes(self):
        for plan in ((1, 1, 1, 1, 1), (2, 2, 2, 2, 1)):
            with self.subTest(plan=plan), self.assertRaises(owner_pack.OwnerPackError):
                package.inspect_archive(archive_bytes(story_manifest(scene_plan=plan)))

    def test_story_rejects_wrong_length(self):
        for per_section in (8, 24, 40):
            with self.subTest(per_section=per_section), self.assertRaises(owner_pack.OwnerPackError):
                package.inspect_archive(archive_bytes(story_manifest(words_per_section=per_section)))

    def test_story_rejects_repeated_image_file(self):
        doc = story_manifest()
        doc["scenes"][3]["asset"] = doc["scenes"][0]["asset"]
        with self.assertRaises(owner_pack.OwnerPackError):
            package.inspect_archive(archive_bytes(doc))

    def test_story_template_is_film_only(self):
        for fmt, template in (("short", "story"), ("podcast", "story")):
            doc = manifest(fmt)
            doc["template"] = template
            with self.subTest(fmt=fmt), self.assertRaises(owner_pack.OwnerPackError):
                package.inspect_archive(archive_bytes(doc))

    def test_identity_sentences_cannot_be_written_into_the_story(self):
        doc = story_manifest()
        doc["script"]["sections"][1]["text"] += " " + STORY_CHANNEL_DEFINITION
        with self.assertRaises(owner_pack.OwnerPackError):
            package.inspect_archive(archive_bytes(doc))


class StoryIdentityTests(unittest.TestCase):
    def _build(self, doc):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = owner_pack.build_artifacts(imported_pack(doc), {"format": "film"}, root)
            plan = json.loads((root / "plan.json").read_text())
            identity = json.loads((root / "narrative-identity.json").read_text())
            return script, plan, identity

    def test_story_gets_the_short_definition_not_the_long_one(self):
        script, plan, identity = self._build(story_manifest())
        joined = " ".join(s["narration"] for s in script["sections"])
        self.assertEqual(joined.count(PRAYER_SENTENCE), 1)
        self.assertEqual(joined.count(STORY_CHANNEL_DEFINITION), 1)
        self.assertEqual(joined.count(LONG_CHANNEL_DEFINITION), 0)
        self.assertEqual(identity["opener"], STORY_CHANNEL_DEFINITION)
        self.assertEqual(plan["narrative_format"], "story_analysis")
        first = script["sections"][0]["narration"]
        self.assertLess(first.index(PRAYER_SENTENCE), first.index(STORY_CHANNEL_DEFINITION))

    def test_ordinary_film_is_unchanged(self):
        script, plan, identity = self._build(manifest("film"))
        joined = " ".join(s["narration"] for s in script["sections"])
        self.assertEqual(joined.count(STORY_CHANNEL_DEFINITION), 0)
        self.assertEqual(plan["narrative_format"], "direct_cinematic")
        self.assertNotEqual(identity["opener"], STORY_CHANNEL_DEFINITION)

    def test_story_text_is_preserved_word_for_word(self):
        doc = story_manifest()
        script, _, identity = self._build(copy.deepcopy(doc))
        joined = " ".join(s["narration"] for s in script["sections"])
        for phrase in (PRAYER_SENTENCE, STORY_CHANNEL_DEFINITION, identity["closer"]):
            if phrase:
                joined = joined.replace(phrase, "")
        self.assertEqual(
            package.spoken_text(joined),
            package.spoken_text(" ".join(package.section_text(s) for s in doc["script"]["sections"])),
        )


class StoryRenderTests(unittest.TestCase):
    """Synthetic WAV + real ffmpeg through the whole pipeline: proves wiring, not how it sounds or looks."""

    def test_story_film_renders_with_the_story_definition_and_voice_direction(self):
        import hashlib
        import os
        from unittest import mock

        from clean_v2 import owner_art
        from clean_v2.contracts import atomic_write_json, compute_brief_sha256
        from clean_v2.media import inspect_final
        from clean_v2.pipeline import CleanV2Pipeline
        from scripts.test_clean_v2 import _passing_audio_mastering, _passing_final_master_qc, _passing_visual_qa
        from scripts.test_ready_production_e2e import LocalVoice

        doc = story_manifest()
        doc["performance"] = {"direction": "راوٍ واحد هادئ بلهجة خليجية طبيعية."}
        pack = imported_pack(doc)
        brief = {"approved_by_user": True, "approved_topic": doc["title"], "format": "film", "language": "ar",
                 "audience": "Arabic-speaking adults", "editorial_intent": "نص معتمد", "research_pack": [],
                 "hard_constraints": ["No fabricated facts."]}
        voice = LocalVoice()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); art = root / "art"; output = root / "out"
            package.extract_archive(archive_bytes(doc), art, expected_sha256=pack["zip_sha256"])
            before = {s["asset"]: hashlib.sha256((art / s["asset"]).read_bytes()).hexdigest() for s in doc["scenes"]}
            atomic_write_json(root / "pack.json", pack); atomic_write_json(root / "brief.json", brief)
            stock = mock.Mock(events=[]); router = mock.Mock(events=[])
            router.route.side_effect = AssertionError("approved script must never call a planning/writing model")
            with mock.patch.dict(os.environ, {owner_pack.OWNER_PACK_ENV: str(root / "pack.json"), owner_art.ART_DIR_ENV: str(art),
                                              "CLEAN_V2_SCENE_REVIEW": "", "CLEAN_V2_SIMPLE_MODE": "true"}), \
                 mock.patch("urllib.request.urlopen", side_effect=AssertionError("no network in this integration test")):
                result = CleanV2Pipeline(
                    router=router, voice_synthesizer=voice, visual_source=owner_art.OwnerArtVisualSource(stock, art),
                    visual_qa=_passing_visual_qa, opening_director=lambda **kw: {"status": "skipped_simple_mode", "slots": []},
                    cinematic_layer=lambda **kw: {"status": "pass", "ai_calls_added": 0},
                    final_master_qc=_passing_final_master_qc, audio_mastering=_passing_audio_mastering,
                ).run(brief_path=root / "brief.json", approved_sha256=compute_brief_sha256(brief), output_dir=output,
                      engine_sha="a" * 40, runner_sha="b" * 40)
            self.assertEqual(result["status"], "pass")
            media = inspect_final(output / "final.mp4")
            self.assertEqual((media["width"], media["height"]), (1920, 1080))
            spoken = " ".join(text for text, _ in voice.spoken)
            self.assertEqual(spoken.count(STORY_CHANNEL_DEFINITION), 1)
            self.assertEqual(spoken.count(LONG_CHANNEL_DEFINITION), 0)
            self.assertTrue(any(k.get("performance_direction") for _, k in voice.spoken))
            timeline = json.loads((output / "timeline-first.json").read_text())
            self.assertIn("channel_identity", [e["kind"] for e in timeline["identity_events"]])
            self.assertEqual(json.loads((output / "plan.json").read_text())["narrative_format"], "story_analysis")
            self.assertEqual(before, {n: hashlib.sha256((art / n).read_bytes()).hexdigest() for n in before})
            stock.acquire.assert_not_called(); router.route.assert_not_called()


if __name__ == "__main__":
    unittest.main()
