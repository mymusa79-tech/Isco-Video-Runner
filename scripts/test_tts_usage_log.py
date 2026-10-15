from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from clean_v2.pipeline import _tts_usage_from_report


class TtsUsageLogTests(unittest.TestCase):
    def test_counts_only_real_requests_and_their_characters(self) -> None:
        report = {"sections": [
            {"chunks": [{"chars": 100, "cache_hit": False}, {"chars": 50, "cache_hit": True}, {"chars": 0}]},
            {"chunks": [{"chars": 30}]},
        ]}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "voice-sections.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            usage = _tts_usage_from_report(path, wire_attempts=3)
        self.assertEqual(usage["tts_requests_sent"], 2)
        self.assertEqual(usage["tts_chars_sent"], 130)
        self.assertEqual(usage["tts_chunks_from_cache"], 1)
        self.assertEqual(usage["tts_wire_attempts_including_retries"], 3)

    def test_missing_report_is_zero_not_an_error(self) -> None:
        usage = _tts_usage_from_report(Path("/nonexistent/voice-sections.json"), wire_attempts=0)
        self.assertEqual((usage["tts_requests_sent"], usage["tts_chars_sent"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
