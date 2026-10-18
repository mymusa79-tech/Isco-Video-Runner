"""Regression checks for owner-approved Iapetus only in Nidaa Shorts."""
from __future__ import annotations

import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from clean_v2.media import (
    GEMINI38_NIDAA_SHORT_PRIMARY_VOICE,
    GEMINI38_NIDAA_SHORT_PROVIDER,
    GEMINI38_NARRATOR_STYLE,
    GEMINI38_PERFORMANCE_STYLES,
    GeminiOnlyVoiceSynthesizer,
)
from clean_v2.pipeline import _voice_performance_mode_for_brief
from clean_v2.tts_cache import _fingerprint, build_binding


def _audio(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\0\0" * 6000)
    return path


class NidaaShortVoiceProfileTests(unittest.TestCase):
    def setUp(self):
        self.brief = {"format": "short", "approved_topic": "متى تبدأ؟"}
        self.plan = {"owner_authored": True, "short_template": "why_reframe"}

    def test_short_uses_new_profile(self):
        self.assertEqual(
            _voice_performance_mode_for_brief(self.brief, self.plan),
            "nidaa_short_why_reframe",
        )

    def test_ala_mahl_keeps_original_mode(self):
        self.assertEqual(
            _voice_performance_mode_for_brief(
                self.brief, self.plan, ready_template="ala_mahl"
            ), "why_reframe"
        )
        self.assertEqual(
            _voice_performance_mode_for_brief(
                {**self.brief, "series_name": "على مهل ☕"}, self.plan
            ), "why_reframe"
        )

    def test_long_and_podcast_have_no_new_prefix(self):
        self.assertEqual(
            _voice_performance_mode_for_brief({"format": "podcast"}, self.plan),
            "podcast_listener_proxy_qa",
        )
        self.assertEqual(
            _voice_performance_mode_for_brief({"format": "film"}, None),
            "direct_cinematic",
        )

    def test_four_short_styles_are_distinct_from_longform(self):
        for name in ("why_reframe", "inner_dialogue", "micro_story", "quote_reflection"):
            self.assertIn("nidaa_short_" + name, GEMINI38_PERFORMANCE_STYLES)
            self.assertNotEqual(
                GEMINI38_PERFORMANCE_STYLES["nidaa_short_" + name],
                GEMINI38_PERFORMANCE_STYLES.get(name, GEMINI38_NARRATOR_STYLE),
            )

    def test_nidaa_content_and_identity_use_iapetus(self):
        events = []
        def fake(_key, _text, path, **kw):
            events.append(kw)
            return _audio(Path(path))
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"CLEAN_V2_TTS_CACHE_PATH": ""}, clear=False
        ), patch("clean_v2.media._gemini38_synthesize", side_effect=fake):
            synth = GeminiOnlyVoiceSynthesizer("test-api-key")
            synth.synthesize("ابدأ بخطوة واحدة.", Path(tmp) / "hook.wav",
                             performance_mode="nidaa_short_why_reframe")
            self.assertEqual(synth.last_provider, GEMINI38_NIDAA_SHORT_PROVIDER)
            self.assertEqual(synth.voice_roles["narrator"], "Iapetus")
            self.assertEqual(events[-1]["primary_voice"], GEMINI38_NIDAA_SHORT_PRIMARY_VOICE)
            synth.synthesize("اللهم صل وسلم.", Path(tmp) / "prayer.wav",
                             performance_mode="nidaa_short_identity")
            self.assertEqual(synth.last_provider, GEMINI38_NIDAA_SHORT_PROVIDER)
            self.assertEqual(synth.voice_roles["narrator"], "Iapetus")
            self.assertEqual(events[-1]["primary_voice"], "Iapetus")
            self.assertEqual(events[-1]["performance_mode"], "nidaa_short_identity")
            self.assertEqual(
                GEMINI38_PERFORMANCE_STYLES.get("nidaa_short_identity", GEMINI38_NARRATOR_STYLE),
                GEMINI38_NARRATOR_STYLE,
            )
            synth.synthesize("هنا نداء اليقظة؛ وعي أوضح للحياة اليومية.", Path(tmp) / "identity.wav",
                             performance_mode="nidaa_short_identity")
            self.assertEqual(synth.voice_roles["narrator"], "Iapetus")
            self.assertEqual(events[-1]["primary_voice"], "Iapetus")
            synth.synthesize("جرّب اليوم خطوة واحدة.", Path(tmp) / "outro.wav",
                             performance_mode="nidaa_short_micro_story")
            self.assertEqual(synth.voice_roles["narrator"], "Iapetus")
            self.assertEqual(events[-1]["primary_voice"], "Iapetus")
            synth.synthesize("نص طويل.", Path(tmp) / "film.wav", performance_mode="direct_cinematic")
            self.assertEqual(synth.voice_roles["narrator"], "Charon")
            self.assertEqual(events[-1]["primary_voice"], "Charon")

    def test_short_cache_changes_with_style_but_legacy_binding_unchanged(self):
        common = dict(
            transcript="جملة واحدة.", model="gemini-3.8-flash-tts",
            primary_voice="Charon", questioner_voice="Orus",
            provider="gemini-3.8:Charon", performance_mode="direct_cinematic",
        )
        original = build_binding(**common)
        self.assertNotIn("style_sha256", original)
        self.assertEqual(original, build_binding(**common, style_sha256=None))
        self.assertEqual(_fingerprint(original), _fingerprint(build_binding(**common)))
        short = {**common, "primary_voice": "Iapetus", "provider": GEMINI38_NIDAA_SHORT_PROVIDER,
                 "performance_mode": "nidaa_short_why_reframe"}
        one = build_binding(**short, style_sha256="a" * 64)
        two = build_binding(**short, style_sha256="b" * 64)
        self.assertNotEqual(_fingerprint(one), _fingerprint(two))

    def test_lite_is_not_an_unapproved_voice_substitution(self):
        with tempfile.TemporaryDirectory() as tmp:
            synth = GeminiOnlyVoiceSynthesizer("test-api-key", tts_model="gemini-3.8-flash-lite-tts")
            with self.assertRaisesRegex(RuntimeError, "Flash TTS only"):
                synth.synthesize("نص.", Path(tmp) / "sample.wav",
                                 performance_mode="nidaa_short_why_reframe")


if __name__ == "__main__":
    unittest.main()
