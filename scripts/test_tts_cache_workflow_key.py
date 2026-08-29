from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = Path(".github/workflows/produce-resilient-v4.yml")


class TtsCacheWorkflowKeyTests(unittest.TestCase):
    def test_tts_cache_save_key_is_unique_per_workflow_attempt(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        primary = (
            "key: tts-section-v1-${{ runner.os }}-"
            "${{ steps.validate_brief.outputs.brief_sha256 }}-"
            "${{ github.run_id }}-${{ github.run_attempt }}"
        )
        legacy = (
            "key: tts-section-v1-${{ runner.os }}-"
            "${{ steps.validate_brief.outputs.brief_sha256 }}-"
            "${{ github.run_id }}\n"
        )

        # One primary key is used for restore and one for save. run_attempt is required
        # because GitHub Actions cache entries are immutable: re-running the same run_id
        # must be able to save newly completed TTS sections under a fresh key.
        self.assertEqual(text.count(primary), 2)
        self.assertNotIn(legacy, text)

        # Keep the compatibility prefix attempt-agnostic so the next attempt/run can
        # restore the most recent compatible cache for the same approved brief.
        restore_prefix = (
            "tts-section-v1-${{ runner.os }}-"
            "${{ steps.validate_brief.outputs.brief_sha256 }}-"
        )
        self.assertGreaterEqual(text.count(restore_prefix), 3)


if __name__ == "__main__":
    unittest.main()
