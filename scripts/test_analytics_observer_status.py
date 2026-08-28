from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.analytics_observer_status import STATUS_FILENAME, observe_post_acceptance_analytics


class AnalyticsObserverStatusTests(unittest.TestCase):
    def test_success_is_persisted_without_release_authority(self) -> None:
        calls = []

        def collector(**kwargs):
            calls.append(kwargs)
            return {"ok": True}

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = observe_post_acceptance_analytics(
                root,
                collector=collector,
                format_hint="film",
                expected_video_id="abc123",
                production_id="v4:1:1",
                binding_source="manual_binding",
            )
            saved = json.loads((root / STATUS_FILENAME).read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "success")
        self.assertEqual(saved["status"], "success")
        self.assertEqual(saved["production_authority"], "none")
        self.assertFalse(saved["release_blocked"])
        self.assertEqual(calls[0]["expected_video_id"], "abc123")

    def test_failure_is_durable_and_non_blocking(self) -> None:
        def collector(**kwargs):
            del kwargs
            raise RuntimeError("sensitive provider detail must not be persisted")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = observe_post_acceptance_analytics(
                root,
                collector=collector,
                format_hint="film",
                expected_video_id=None,
                production_id=None,
                binding_source=None,
            )
            raw = (root / STATUS_FILENAME).read_text(encoding="utf-8")
            saved = json.loads(raw)

        self.assertEqual(result["status"], "error")
        self.assertEqual(saved["error_type"], "RuntimeError")
        self.assertFalse(saved["release_blocked"])
        self.assertNotIn("sensitive provider detail", raw)

    def test_status_write_is_atomic_and_replaces_prior_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            observe_post_acceptance_analytics(
                root,
                collector=lambda **kwargs: None,
                format_hint="film",
                expected_video_id="one",
                production_id="p1",
                binding_source="binding",
            )
            observe_post_acceptance_analytics(
                root,
                collector=lambda **kwargs: (_ for _ in ()).throw(ValueError("x")),
                format_hint="film",
                expected_video_id="two",
                production_id="p2",
                binding_source="binding",
            )
            saved = json.loads((root / STATUS_FILENAME).read_text(encoding="utf-8"))
            leftovers = list(root.glob("*.tmp"))

        self.assertEqual(saved["status"], "error")
        self.assertEqual(saved["expected_video_id"], "two")
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
