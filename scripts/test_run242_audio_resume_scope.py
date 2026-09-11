from __future__ import annotations

import unittest

from scripts.short_finishing_capabilities import (
    bind_audio_resume_short_capability,
    current_short_finishing_gemini_for_audio,
)


class Run242AudioResumeScopeTests(unittest.TestCase):
    def test_moment_resume_rebinds_captured_gemini_only_inside_scope(self) -> None:
        self.assertIsNone(current_short_finishing_gemini_for_audio())
        with bind_audio_resume_short_capability(
            fmt="moment",
            gemini="captured-gemini",
            pexels="captured-pexels",
            pixabay="captured-pixabay",
        ):
            self.assertEqual(
                current_short_finishing_gemini_for_audio(),
                "captured-gemini",
            )
        self.assertIsNone(current_short_finishing_gemini_for_audio())

    def test_long_resume_never_enters_short_capability_scope(self) -> None:
        with bind_audio_resume_short_capability(
            fmt="film",
            gemini="captured-long-gemini",
            pexels="captured-long-pexels",
            pixabay=None,
        ):
            self.assertIsNone(current_short_finishing_gemini_for_audio())


if __name__ == "__main__":
    unittest.main()
