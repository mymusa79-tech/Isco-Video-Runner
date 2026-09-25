from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2.contracts import ContractError, SUPPORTED_FORMATS, validate_plan
from clean_v2.identity_sequence import (
    LONG_CHANNEL_DEFINITION,
    PRAYER_SENTENCE,
    assert_spoken_identity,
    inject_spoken_identity,
)
from clean_v2.media import GeminiPrimaryNabraFallbackSynthesizer
from clean_v2.pipeline import _planning_prompt, _script_prompt
from scripts import clean_v2_release_delivery as delivery
from scripts.telegram_clean_v2_control import (
    _request_hash,
    _scope_research_instruction,
    materialize_brief,
    scope_keyboard,
)
from scripts.telegram_clean_v2_notify import milestone_messages, started_text


class _FakeNabra:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def synthesize(self, transcript: str, output_path: Path) -> Path:
        self.calls.append(transcript)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"RIFF" + b"0" * 2048)
        return output_path


class PodcastFormatTests(unittest.TestCase):
    def _plan(self, count: int) -> dict:
        return {
            "title": "عنوان",
            "promise": "وعد واضح",
            "cta": "شاركها مع من يحتاجها.",
            "sections": [
                {
                    "id": f"s{index}",
                    "heading": f"قسم {index}",
                    "purpose": f"تقدم الفكرة في القسم {index}",
                    "visual_query_en": f"quiet lived in room detail {index}",
                }
                for index in range(1, count + 1)
            ],
        }

    def test_podcast_is_a_first_class_clean_v2_format_with_flexible_sections(self) -> None:
        self.assertIn("podcast", SUPPORTED_FORMATS)
        self.assertEqual(len(validate_plan(self._plan(2), {"format": "podcast"})["sections"]), 2)
        self.assertEqual(len(validate_plan(self._plan(5), {"format": "podcast"})["sections"]), 5)
        with self.assertRaisesRegex(ContractError, "between 2 and 5 for podcast"):
            validate_plan(self._plan(1), {"format": "podcast"})
        with self.assertRaisesRegex(ContractError, "between 2 and 5 for podcast"):
            validate_plan(self._plan(6), {"format": "podcast"})

    def test_podcast_prompt_prioritizes_depth_audio_only_and_sparse_visuals(self) -> None:
        brief = {
            "approved_by_user": True,
            "approved_topic": "لماذا نعود إلى عادة نعرف أنها تؤذينا؟",
            "format": "podcast",
            "language": "ar",
            "research_pack": [],
        }
        planning = _planning_prompt(brief)
        script = _script_prompt(brief, self._plan(3))
        self.assertIn("genuinely worthwhile central question", planning)
        self.assertIn("generic self-help", planning)
        self.assertIn("ONE visual beat per section", planning)
        self.assertIn("audio must", planning)
        self.assertIn("neutral female narrator", script)
        self.assertIn("local Nabra af_msa", script)
        self.assertIn("Never invent first-person", script)
        self.assertIn("Do not write toward a word-count or duration target", script)
        self.assertIn("audio alone", script)

    def test_podcast_reuses_long_identity_without_a_new_identity_system(self) -> None:
        sections = [
            {"id": "s1", "narration": "لماذا نعود إلى ما قررنا تركه؟ نبدأ من وظيفة السلوك نفسه."},
            {"id": "s2", "narration": "عندما نفهم الوظيفة يصبح التغيير أوضح."},
        ]
        closer = "وهنا تنتهي الفكرة، لا الرحلة."
        inject_spoken_identity(sections, fmt="podcast", closer=closer)
        joined = "\n".join(item["narration"] for item in sections)
        self.assertEqual(joined.count(PRAYER_SENTENCE), 1)
        self.assertEqual(joined.count(LONG_CHANNEL_DEFINITION), 1)
        self.assertTrue(sections[-1]["narration"].endswith(closer))
        assert_spoken_identity(sections, fmt="podcast", closer=closer)


class PodcastNabraRoutingTests(unittest.TestCase):
    def test_podcast_primary_lock_never_attempts_charon_and_is_not_fallback(self) -> None:
        fake = _FakeNabra()
        synth = GeminiPrimaryNabraFallbackSynthesizer("", nabra=fake)
        synth.activate_full_run_nabra_primary()
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "clean_v2.media._legacy_voice_identity", return_value=("Charon", "Orus")
        ), mock.patch(
            "clean_v2.media._legacy_gemini_synthesize",
            side_effect=AssertionError("Charon must not be called for Podcast"),
        ):
            result = synth.synthesize("هذا نص بودكاست عربي.", Path(tmp) / "voice.wav")
            self.assertTrue(result.is_file())
        self.assertEqual(fake.calls, ["هذا نص بودكاست عربي."])
        self.assertEqual(synth.last_provider, "nabra:af_msa")
        self.assertFalse(synth.fallback_used)
        self.assertEqual(synth.charon_attempts, 0)
        self.assertEqual(synth.voice_approval_status, "user_selected_primary")


class PodcastTelegramTests(unittest.TestCase):
    def test_client_has_one_podcast_choice(self) -> None:
        callbacks = [
            button["callback_data"]
            for row in scope_keyboard()
            for button in row
        ]
        self.assertIn("scope:podcast", callbacks)
        self.assertIn("سؤال مركزي حقيقي", _scope_research_instruction("podcast"))

    def test_materialized_podcast_brief_keeps_choice_simple_and_marks_female_narration(self) -> None:
        request = {
            "schema_version": 1,
            "request_id": "req-podcast",
            "source": "clean_v2_telegram_editorial_lite",
            "scope": "podcast",
            "approved_by_user": True,
            "approved_topic": "لماذا نعود إلى عادة نعرف أنها تؤذينا؟",
            "research_pack": [],
            "idea_id": "idea-podcast",
            "selected_at": "2026-09-25T00:00:00Z",
            "status": "dispatched",
            "confirmed_at": "2026-09-25T00:01:00Z",
            "dispatched_at": "2026-09-25T00:02:00Z",
        }
        request["request_sha256"] = _request_hash(request)
        state = {"requests": {"req-podcast": request}}
        with tempfile.TemporaryDirectory() as tmp:
            brief = materialize_brief(
                state,
                "req-podcast",
                request["request_sha256"],
                "podcast",
                Path(tmp) / "brief.json",
            )
        self.assertEqual(brief["format"], "podcast")
        self.assertIn("راوية أنثوية محايدة", brief["editorial_intent"])
        self.assertTrue(any("female narrator" in item for item in brief["hard_constraints"]))

    def test_podcast_delivery_and_status_keep_the_same_shared_paths(self) -> None:
        root = Path("/tmp/clean-v2-podcast-test")
        self.assertEqual(delivery._target_dirs(root, "podcast"), [("podcast", root / "podcast")])
        self.assertIn("بودكاست", started_text(scope="podcast", topic="موضوع", run_url=""))
        messages = dict(
            milestone_messages(
                {"stages": [{"name": "planning", "status": "pass"}]},
                set(),
                kind="podcast",
            )
        )
        self.assertIn("🎙️ البودكاست", messages["planning"])


if __name__ == "__main__":
    unittest.main()
