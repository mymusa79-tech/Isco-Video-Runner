"""Editorial continuity checks for existing Film/Podcast prompts.

No new audit, provider call, script-duration gate or structural format is added.
"""
from __future__ import annotations

import unittest

from clean_v2.pipeline import (
    LONGFORM_RETENTION_PREFLIGHT,
    PODCAST_GEMINI_PERFORMANCE_GUIDANCE,
    _PODCAST_FIXED_PROFILE,
    _script_prompt,
)


class Run125LongformAnswerContinuityTests(unittest.TestCase):
    @staticmethod
    def _prompt(fmt: str) -> str:
        brief = {
            "format": fmt,
            "approved_by_user": True,
            "approved_topic": "لماذا نؤجل ما نعرف أنه مهم؟",
            "language": "ar",
            "research_pack": [],
        }
        plan = {
            "title": "فهم التأجيل",
            "promise": "فهم الفارق بين المعرفة والفعل",
            "sections": [
                {"id": "s1", "purpose": "السؤال المركزي"},
                {"id": "s2", "purpose": "السبب"},
                {"id": "s3", "purpose": "الخلاصة"},
            ],
            "cta": "",
        }
        return _script_prompt(brief, plan, visual_story={"beats": []})

    def test_shared_film_podcast_progression_pays_back_opening_early(self) -> None:
        self.assertIn("first substantive content after identity", LONGFORM_RETENTION_PREFLIGHT)
        self.assertIn("real partial answer", LONGFORM_RETENTION_PREFLIGHT)
        for fmt in ("film", "podcast"):
            self.assertIn("PAY DOWN that exact hook", self._prompt(fmt))

    def test_film_gives_value_beyond_the_hook_without_new_duration_gate(self) -> None:
        prompt = self._prompt("film")
        self.assertIn("observable insight", prompt)
        self.assertIn("powerful hook followed by generic advice is incomplete", prompt)
        self.assertIn("never an acceptance gate", prompt)
        self.assertNotIn("listener's PRIVATE INNER QUESTION", prompt)

    def test_podcast_question_is_private_listener_doubt_not_interview(self) -> None:
        self.assertIn("listener's PRIVATE INNER QUESTION", PODCAST_GEMINI_PERFORMANCE_GUIDANCE)
        self.assertIn("exact unresolved implication", PODCAST_GEMINI_PERFORMANCE_GUIDANCE)
        self.assertIn("formal interviewer", PODCAST_GEMINI_PERFORMANCE_GUIDANCE)
        self.assertIn("After an answer, A can voice only the next genuine uncertainty", _PODCAST_FIXED_PROFILE["writing"])

    def test_podcast_answer_begins_with_specific_response_then_depth(self) -> None:
        prompt = self._prompt("podcast")
        self.assertIn("Except for the short pre-prayer identity bridge", prompt)
        self.assertIn("first one or two TOPIC sentences", prompt)
        self.assertIn("approved mechanism", prompt)
        self.assertIn("concrete everyday example", prompt)
        self.assertIn("not a prompt engineered", prompt)
        self.assertIn("B carries the substance", prompt)


if __name__ == "__main__":
    unittest.main()
