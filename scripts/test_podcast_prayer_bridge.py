import io
import tempfile
import wave
import unittest
from pathlib import Path
from unittest.mock import patch

from clean_v2.identity_sequence import (
    PODCAST_FALLBACK_BRIDGE,
    PODCAST_PRAYER_MARKER,
    PRAYER_SENTENCE,
    inject_spoken_identity,
)
from clean_v2.media import GeminiOnlyVoiceSynthesizer
from clean_v2.pipeline import _synthesize_sectioned_voice
from clean_v2.pipeline import PODCAST_BRIDGE_GUIDANCE, _podcast_script_word_count, _script_prompt

HOOK = "A: لماذا نؤجل ما نعرف أنه صحيح؟"
ANSWER = "B: لأن الدماغ يفضل الراحة الآن على المكسب لاحقاً."


def _run(narration, extra=None):
    sections = [{"narration": narration}] + (extra or [])
    inject_spoken_identity(sections, fmt="podcast")
    return sections


class BridgeInjectionTests(unittest.TestCase):
    def test_valid_marker_keeps_writer_bridge_and_swaps_prayer(self):
        out = _run(f"{HOOK} B: سأشرح لك السبب بهدوء. {PODCAST_PRAYER_MARKER} {ANSWER}")[0]["narration"]
        self.assertEqual(out.count(PRAYER_SENTENCE), 1)
        self.assertNotIn(PODCAST_PRAYER_MARKER, out)
        self.assertNotIn(PODCAST_FALLBACK_BRIDGE, out)
        self.assertLess(out.index("سأشرح لك السبب"), out.index(PRAYER_SENTENCE))
        self.assertLess(out.index(PRAYER_SENTENCE), out.index("لأن الدماغ"))

    def test_missing_marker_uses_fallback_bridge_and_keeps_prayer(self):
        out = _run(f"{HOOK} {ANSWER}")[0]["narration"]
        self.assertEqual(out.count(PRAYER_SENTENCE), 1)
        self.assertIn(PODCAST_FALLBACK_BRIDGE, out)
        self.assertLess(out.index(PODCAST_FALLBACK_BRIDGE), out.index(PRAYER_SENTENCE))

    def test_misplaced_marker_is_stripped_and_falls_back(self):
        extra = [{"narration": f"B: تتمة {PODCAST_PRAYER_MARKER} الكلام."}]
        sections = _run(f"{HOOK} {ANSWER}", extra)
        joined = " ".join(s["narration"] for s in sections)
        self.assertNotIn(PODCAST_PRAYER_MARKER, joined)
        self.assertEqual(joined.count(PRAYER_SENTENCE), 1)
        self.assertIn(PODCAST_FALLBACK_BRIDGE, joined)

    def test_bridge_with_question_or_prayer_text_is_rejected(self):
        for bad in ("هل تعرف السبب؟", f"{PRAYER_SENTENCE}"):
            out = _run(f"{HOOK} B: {bad} {PODCAST_PRAYER_MARKER} {ANSWER}")[0]["narration"]
            self.assertNotIn(PODCAST_PRAYER_MARKER, out)
            self.assertEqual(out.count(PRAYER_SENTENCE), 1)

    def test_too_long_bridge_is_rejected(self):
        long = " ".join(["كلمة"] * 20) + "."
        out = _run(f"{HOOK} B: {long} {PODCAST_PRAYER_MARKER} {ANSWER}")[0]["narration"]
        self.assertIn(PODCAST_FALLBACK_BRIDGE, out)

    def test_two_markers_rejected(self):
        out = _run(f"{HOOK} B: سأجيب الآن. {PODCAST_PRAYER_MARKER} {ANSWER} {PODCAST_PRAYER_MARKER}")[0]["narration"]
        self.assertNotIn(PODCAST_PRAYER_MARKER, out)
        self.assertEqual(out.count(PRAYER_SENTENCE), 1)

    def test_writer_prayer_text_is_not_duplicated(self):
        out = _run(f"{HOOK} B: سأشرح لك السبب. {PRAYER_SENTENCE} {PODCAST_PRAYER_MARKER} {ANSWER}")[0]["narration"]
        self.assertEqual(out.count(PRAYER_SENTENCE), 1)


class BridgePromptTests(unittest.TestCase):
    def _brief(self, fmt):
        return {"approved_by_user": True, "approved_topic": "موضوع", "format": fmt,
                "language": "ar", "research_pack": []}

    def _plan(self):
        return {"title": "t", "promise": "p", "cta": "c", "narrative_format": "dialogue_qa",
                "sections": [{"id": "s1", "heading": "h", "purpose": "p", "visual_query_en": "q"}]}

    def test_podcast_prompt_has_bridge_guidance_and_never_names_the_prayer_text(self):
        prompt = _script_prompt(self._brief("podcast"), self._plan())
        self.assertIn(PODCAST_BRIDGE_GUIDANCE, prompt)
        self.assertIn(PODCAST_PRAYER_MARKER, PODCAST_BRIDGE_GUIDANCE)
        self.assertNotIn(PRAYER_SENTENCE, PODCAST_BRIDGE_GUIDANCE)

    def test_word_count_ignores_marker(self):
        a = _podcast_script_word_count({"sections": [{"narration": "كلمة كلمة كلمة"}]})
        b = _podcast_script_word_count({"sections": [{"narration": f"كلمة {PODCAST_PRAYER_MARKER} كلمة كلمة"}]})
        self.assertEqual(a, b)


def _wav():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * 4096)
    return buf.getvalue()


class BridgeVoiceUnitTests(unittest.TestCase):
    def test_bridge_is_voiced_by_charon_with_prayer_after_question(self):
        calls = []

        def tts(_key, transcript, path, *, model, **_kw):
            calls.append(transcript)
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(_wav())
            return Path(path)

        def concat(inputs, target, *a, **k):
            Path(target).write_bytes(_wav())
            return Path(target)

        sections = _run(f"{HOOK} B: سأشرح لك السبب بهدوء. {PODCAST_PRAYER_MARKER} {ANSWER}")
        sections[0]["id"] = "s1"
        with tempfile.TemporaryDirectory() as tmp:
            synth = GeminiOnlyVoiceSynthesizer("gemini-test-key")
            with patch("clean_v2.media._gemini38_synthesize", side_effect=tts), patch(
                "clean_v2.media.time.sleep"
            ), patch("clean_v2.pipeline.concat_wav_parts", side_effect=concat):
                _synthesize_sectioned_voice(
                    synth, sections, Path(tmp) / "n.wav", fmt="podcast", require_charon_only=True
                )
        joined = "\n".join(calls)
        self.assertEqual(joined.count(PRAYER_SENTENCE), 1)
        self.assertLess(joined.index("سأشرح لك السبب"), joined.index(PRAYER_SENTENCE))
        question_call = next(c for c in calls if "لماذا نؤجل" in c)
        self.assertNotIn("سأشرح لك السبب", question_call)
        self.assertNotIn(PRAYER_SENTENCE, question_call)


if __name__ == "__main__":
    unittest.main()
