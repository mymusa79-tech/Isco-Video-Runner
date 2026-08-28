from __future__ import annotations

import unittest
from pathlib import Path


class AnalyticsLiveObservabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = Path("scripts/run_v3_voice.py").read_text(encoding="utf-8")

    def test_live_entrypoint_uses_observable_post_acceptance_wrapper(self) -> None:
        self.assertIn(
            "from scripts.analytics_observer_status import observe_post_acceptance_analytics",
            self.text,
        )
        manifest = self.text.index("manifest = _write_production_manifest")
        observer = self.text.index("analytics_status = observe_post_acceptance_analytics")
        telemetry = self.text.index("telemetry_path = write_planning_telemetry(out)", observer)
        self.assertLess(manifest, observer)
        self.assertLess(observer, telemetry)

    def test_analytics_status_is_embedded_in_durable_telemetry(self) -> None:
        self.assertIn("analytics_status: dict | None = None", self.text)
        self.assertIn('data["analytics_observer_status"] = analytics_status', self.text)
        self.assertIn("analytics_status=analytics_status", self.text)

    def test_silent_analytics_exception_swallow_is_gone(self) -> None:
        legacy = "collect_latest_video_metrics_from_env(\n            format_hint=plan.format"
        self.assertNotIn("except Exception:\n        pass", self.text[self.text.find(legacy):])


if __name__ == "__main__":
    unittest.main()
