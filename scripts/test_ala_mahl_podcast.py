"""Opt-in Ala Mahl podcast regression: imported ZIP, voices and identity are isolated."""
from __future__ import annotations

import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from clean_v2 import identity_sequence as identity, media, owner_pack
from clean_v2.pipeline import _voice_performance_mode_for_brief
from clean_v2.production_package import validate_manifest
from scripts.test_production_package import imported_pack, manifest


def _wav(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\\0\\0" * 7000)
    return path


class AlaMahlPodcastTests(unittest.TestCase):
    def test_new_template_accepts_same_safe_podcast_contract(self):
        new_doc = manifest("podcast", template="ala_mahl_dialogue")
        old_doc = manifest("podcast")
        self.assertEqual(validate_manifest(new_doc)["template"], "ala_mahl_dialogue")
        self.assertEqual(validate_manifest(old_doc)["template"], "dialogue_qa")
        bad = manifest("podcast", template="made_up")
        with self.assertRaises(Exception):
            validate_manifest(bad)

    def test_owner_zip_build_uses_legacy_dialogue_engine_without_rewriting(self):
        doc = manifest("podcast", template="ala_mahl_dialogue")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = owner_pack.build_artifacts(imported_pack(doc), {"format": "podcast"}, root)
            self.assertEqual(len(script["sections"]), 2)
            self.assertIn("A:", script["sections"][0]["narration"])
            self.assertIn("B:", script["sections"][0]["narration"])
            self.assertEqual(__import__("json").loads((root / "production.json").read_text())["template"],
                             "ala_mahl_dialogue")
            self.assertEqual(__import__("json").loads((root / "plan.json").read_text())["narrative_format"],
                             "dialogue_qa")

    def test_two_perspective_A_can_make_a_real_objection(self):
        from clean_v2.pipeline import (
            _validate_ala_mahl_podcast_dialogue_script,
            _validate_podcast_listener_proxy_script,
        )
        long_objection = "لماذا نفترض أنه مخطئ؟ " + " ".join(["ربما كان لديه سبب"] * 8) + "؟"
        sections = [
            {"narration": "A: لماذا رفض طلب صديقه؟ B: " + " ".join(["ربما خاف من عدم القدرة."] * 14)},
            {"narration": "A: " + long_objection + " B: " + " ".join(["هنا علينا أن نسأل عن الملابسات."] * 14)},
        ]
        report = _validate_ala_mahl_podcast_dialogue_script({"sections": sections})
        self.assertEqual(report["voices"]["A"], "Iapetus")
        with self.assertRaisesRegex(RuntimeError, "question_too_long"):
            _validate_podcast_listener_proxy_script({"sections": sections})

    def test_podcast_modes_are_not_interchanged(self):
        self.assertEqual(_voice_performance_mode_for_brief({"format": "podcast"}),
                         "podcast_listener_proxy_qa")
        self.assertEqual(_voice_performance_mode_for_brief(
            {"format": "podcast"}, ready_template="ala_mahl_dialogue"),
            "ala_mahl_podcast_dialogue")
        self.assertEqual(_voice_performance_mode_for_brief({"format": "short"},
                         {"owner_authored": True, "short_template": "micro_story"},
                         ready_template="ala_mahl"), "micro_story")

    def test_a_iapetus_b_charon_but_legacy_a_orus(self):
        events = []

        def fake(_key, _text, path, **kwargs):
            events.append(kwargs)
            return _wav(Path(path))

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"CLEAN_V2_TTS_CACHE_PATH": ""}, clear=False
        ), patch("clean_v2.media._gemini38_synthesize", side_effect=fake):
            synth = media.GeminiOnlyVoiceSynthesizer("test-api-key")
            dialogue = "A: أهذا قرار عادل؟ B: يعتمد على ما نعرفه."
            synth.synthesize(dialogue, Path(tmp) / "ala.wav",
                             performance_mode="ala_mahl_podcast_dialogue")
            self.assertEqual(events[-1]["questioner_voice"], "Iapetus")
            self.assertEqual(events[-1]["primary_voice"], "Charon")
            self.assertEqual(synth.voice_roles["questioner"], "Iapetus")
            self.assertEqual(synth.voice_roles["responder"], "Charon")
            self.assertEqual(synth.voice_reference_profile,
                             media.GEMINI38_ALA_MAHL_REFERENCE_PROFILE)
            synth.synthesize(dialogue, Path(tmp) / "legacy.wav",
                             performance_mode="podcast_listener_proxy_qa")
            self.assertEqual(events[-1]["questioner_voice"], "Orus")
            self.assertEqual(events[-1]["primary_voice"], "Charon")
            self.assertEqual(synth.voice_roles["questioner"], "Orus")

    def test_new_duo_never_falls_back_to_unapproved_lite(self):
        with tempfile.TemporaryDirectory() as tmp:
            synth = media.GeminiOnlyVoiceSynthesizer(
                "test-api-key", tts_model="gemini-3.8-flash-lite-tts")
            with self.assertRaisesRegex(RuntimeError, "requires Flash TTS"):
                synth.synthesize("A: لماذا؟ B: لنتحدث.", Path(tmp) / "x.wav",
                                 performance_mode="ala_mahl_podcast_dialogue")

    def test_podcast_identity_separate_old_path_untouched(self):
        ala_assets = (Path("ala-intro.mp4"), Path("ala-outro.mp4"))
        legacy_assets = (Path("old-intro.mp4"), Path("old-outro.mp4"))
        with patch.object(identity, "_ensure_ala_mahl_podcast_assets",
                          return_value=ala_assets) as ala, patch.object(
                          identity, "_ensure_podcast_v8_assets",
                          return_value=legacy_assets) as legacy:
            self.assertEqual(identity.identity_asset_paths(
                "podcast", template="ala_mahl_dialogue")["intro"], ala_assets[0])
            self.assertEqual(identity.identity_asset_paths(
                "podcast", template="dialogue_qa")["intro"], legacy_assets[0])
            self.assertEqual(ala.call_count, 1)
            self.assertEqual(legacy.call_count, 1)


if __name__ == "__main__":
    unittest.main()
