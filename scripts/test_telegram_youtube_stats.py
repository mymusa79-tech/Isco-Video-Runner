from __future__ import annotations

import unittest

from scripts import telegram_youtube_stats as stats


class TelegramYoutubeStatsTests(unittest.TestCase):
    def _live(self):
        return {
            "fetched_at": "2026-08-26T08:45:00+00:00",
            "channel_id": "UC-test",
            "channel_title": "نداء اليقظة",
            "hidden_subscriber_count": False,
            "subscribers": 12,
            "views": 3456,
            "videos_count": 9,
            "videos": [
                {"id": "short1", "title": "شورت", "published_at": "2026-08-26T07:00:00Z", "duration_seconds": 45, "is_short_approx": True, "views": 1700, "likes": 60, "comments": 7},
                {"id": "long1", "title": "حلقة", "published_at": "2026-08-25T10:00:00Z", "duration_seconds": 900, "is_short_approx": False, "views": 400, "likes": 20, "comments": 5},
            ],
        }

    def test_duration_parser_supports_hours_minutes_seconds(self):
        self.assertEqual(stats._duration_seconds("PT45S"), 45)
        self.assertEqual(stats._duration_seconds("PT2M30S"), 150)
        self.assertEqual(stats._duration_seconds("PT1H2M3S"), 3723)

    def test_latest_short_and_long_are_separated(self):
        live = self._live()
        short_text, short_url = stats.render_latest(live, short=True)
        long_text, long_url = stats.render_latest(live, short=False)
        self.assertIn("1.7K", short_text)
        self.assertIn("تصنيف Short", short_text)
        self.assertEqual(short_url, "https://youtu.be/short1")
        self.assertIn("400", long_text)
        self.assertEqual(long_url, "https://youtu.be/long1")

    def test_overview_is_small_operational_summary(self):
        text = stats.render_overview(self._live())
        self.assertIn("المشتركون: 12", text)
        self.assertIn("3.5K", text)
        self.assertIn("بتوقيت عُمان", text)
        self.assertIn("YouTube Studio", text)

    def test_snapshot_history_is_bounded_and_replaces_same_minute(self):
        state = {}
        live = self._live()
        stats.record_snapshot(state, live)
        live2 = dict(live, subscribers=13, fetched_at="2026-08-26T08:45:30+00:00")
        stats.record_snapshot(state, live2)
        self.assertEqual(len(state[stats.SNAPSHOT_STATE_KEY]), 1)
        self.assertEqual(state[stats.SNAPSHOT_STATE_KEY][0]["subscribers"], 13)

    def test_period_without_baseline_is_explicitly_approximate(self):
        text = stats.render_period(self._live(), {}, days=1)
        self.assertIn("خط الأساس ما زال يتكوّن", text)
        self.assertIn("رفعات ضمن الفترة", text)
        self.assertIn("ليست YouTube Analytics الدقيقة", text)

    def test_period_uses_snapshot_before_period_start(self):
        state = {
            stats.SNAPSHOT_STATE_KEY: [
                {"at": "2026-08-25T19:59:00+00:00", "subscribers": 10, "views": 3000, "videos_count": 8}
            ]
        }
        text = stats.render_period(self._live(), state, days=1)
        self.assertIn("+456", text)
        self.assertIn("+2", text)
        self.assertIn("+1", text)


if __name__ == "__main__":
    unittest.main()
