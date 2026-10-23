"""Opt-in Ala Mahl podcast must not change legacy Outside the Text behavior."""
from __future__ import annotations

import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from clean_v2.identity_sequence import (
    _build_ala_mahl_podcast_card,
    identity_asset_paths,
)
from clean_v2.media import (
    GEMINI38_LITE_TTS_MODEL,
    GeminiOnlyVoiceSynthesizer,
)
from clean_v2.pipeline import (
    _validate_podcast_listener_proxy_script,
    _voice_performance_mode_for_brief,
)
from clean_v2.production_package import validate_manifest
from scripts.test_production_package import manifest


def write_wav(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\\0\\0" * 4096)
    return path


class AlaMahlPodcastTests(unittest.TestCase):
    def test_manifest_uses_opt_in_template_only_for_podcast(self):
        ala = manifest("podcast", template="ala_mahl_podcast")
        original = manifest("podcast")
        self.assertEqual(validate_manifest(ala)["template"], "ala_mahl_podcast")
        self.assertEqual(validate_manifest(original)["template"], "dialogue_qa")
        old_short = manifest("short", template="ala_mahl")
        self.assertEqual(validate_manifest(old_short)["template"], "ala_mahl")
        with self.assertRaises(Exception):
            invalid = manifest("short", template="ala_mahl_podcast")
            validate_manifest(invalid)

    def test_ala_allows_real_participant_not_dominating_dialogue(self):
        a = " ".join(["أريد أن أفهم هذا الموقف"] * 5) + " لماذا رفض المساعدة؟"
        b = " ".join(["لننظر إلى مسؤولياته وموقف صديقه قبل إصدار الحكم"] * 7) + "."
        script = {"sections": [{"narration": f"A: {a} B: {b}"}]}
        self.assertEqual(
            _validate_podcast_listener_proxy_script(script, mode="ala_mahl_podcast")["voices"],
            {"A": "Iapetus", "B": "Charon"},
        )
        with self.assertRaisesRegex(RuntimeError, "question_too_long"):
            _validate_podcast_listener_proxy_script(script)
        with self.assertRaisesRegex(RuntimeError, "unknown_mode"):
            _validate_podcast_listener_proxy_script(script, mode="invalid")

    def test_mode_only_changes_on_explicit_ready_template(self):
        brief = {"format": "podcast"}
        self.assertEqual(_voice_performance_mode_for_brief(brief), "podcast_listener_proxy_qa")
        self.assertEqual(
            _voice_performance_mode_for_brief(brief, ready_template="ala_mahl_podcast"),
            "ala_mahl_podcast_dialogue",
        )

    def test_voice_roster_is_separate_and_cache_key_includes_mode(self):
        calls = []
        def fake(_key, _transcript, target, **kwargs):
            calls.append(kwargs)
            return write_wav(Path(target))
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"CLEAN_V2_TTS_CACHE_PATH": ""}, clear=False
        ), patch("clean_v2.media._gemini38_synthesize", side_effect=fake):
            speaker = GeminiOnlyVoiceSynthesizer("test-key")
            transcript = "A: ما الذي حدث فعلًا؟ B: لا نعرف كل التفاصيل."
            speaker.synthesize(transcript, Path(tmp) / "ala.wav",
                               performance_mode="ala_mahl_podcast_dialogue")
            self.assertEqual((calls[-1]["questioner_voice"], calls[-1]["primary_voice"]),
                             ("Iapetus", "Charon"))
            self.assertEqual(speaker.voice_roles["questioner"], "Iapetus")
            speaker.synthesize(transcript, Path(tmp) / "outside.wav",
                               performance_mode="podcast_listener_proxy_qa")
            self.assertEqual((calls[-1]["questioner_voice"], calls[-1]["primary_voice"]),
                             ("Orus", "Charon"))
            self.assertEqual(speaker.voice_roles["questioner"], "Orus")

    def test_ready_artifacts_keep_approved_text_and_template(self):
        import json
        from clean_v2 import owner_pack
        from clean_v2.identity_sequence import PRAYER_SENTENCE
        from scripts.test_production_package import imported_pack
        doc = manifest("podcast", template="ala_mahl_podcast")
        pack = imported_pack(doc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = owner_pack.build_artifacts(pack, {"format": "podcast"}, root)
            plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
            ready = json.loads((root / "production.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["narrative_format"], "dialogue_qa")
            self.assertEqual(ready["template"], "ala_mahl_podcast")
            joined = " ".join(s["narration"] for s in script["sections"])
            self.assertEqual(joined.count(PRAYER_SENTENCE), 1)
            self.assertNotIn("خارج النص", joined)
            self.assertNotIn("نداء اليقظة", joined)


    def test_unapproved_lite_voice_pair_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            speaker = GeminiOnlyVoiceSynthesizer("test-key", tts_model=GEMINI38_LITE_TTS_MODEL)
            with self.assertRaisesRegex(RuntimeError, "requires Flash TTS"):
                speaker.synthesize("A: ماذا حدث؟ B: حدث اختلاف.", Path(tmp) / "out.wav",
                                   performance_mode="ala_mahl_podcast_dialogue")

    def test_intro_and_outro_only_reference_ala_mahl(self):
        with patch("clean_v2.identity_sequence._ensure_ala_mahl_podcast_assets") as ala, patch(
            "clean_v2.identity_sequence._ensure_podcast_v8_assets"
        ) as outside:
            ala.return_value = (Path("ala_intro.mp4"), Path("ala_outro.mp4"))
            outside.return_value = (Path("v8_intro.mp4"), Path("v8_outro.mp4"))
            self.assertEqual(identity_asset_paths("podcast", template="ala_mahl_podcast")["intro"].name,
                             "ala_intro.mp4")
            self.assertEqual(identity_asset_paths("podcast", template="dialogue_qa")["intro"].name,
                             "v8_intro.mp4")
        card = _build_ala_mahl_podcast_card(kind="intro")
        self.assertEqual(card.size, (1280, 720))


if __name__ == "__main__":
    unittest.main()
