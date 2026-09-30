from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.youtube_learning_lite import (
    STATE_KEY,
    build_insights,
    learning_evidence_line,
    learning_memo,
    refresh_state,
)


ROOT = Path(__file__).resolve().parents[1]


def _analytics_report(video_id: str) -> dict:
    values = {
        "s1": [120, 300.0, 30.0, 72.0, 2],
        "s2": [100, 250.0, 28.0, 68.0, 1],
        "s3": [140, 340.0, 32.0, 76.0, 3],
    }[video_id]
    return {
        "columnHeaders": [
            {"name": "views"},
            {"name": "estimatedMinutesWatched"},
            {"name": "averageViewDuration"},
            {"name": "averageViewPercentage"},
            {"name": "subscribersGained"},
        ],
        "rows": [values],
    }


def _retention_report(video_id: str) -> dict:
    shift = {"s1": 0.00, "s2": -0.03, "s3": 0.03}[video_id]
    return {
        "columnHeaders": [
            {"name": "elapsedVideoTimeRatio"},
            {"name": "audienceWatchRatio"},
        ],
        "rows": [
            [0.0, 1.00 + shift],
            [0.1, 0.72 + shift],
            [0.5, 0.61 + shift],
            [1.0, 0.42 + shift],
        ],
    }


class YouTubeLearningLiteTests(unittest.TestCase):
    def _videos(self) -> list[dict]:
        return [
            {
                "id": "s1",
                "title": "فكرة أولى",
                "published_at": "2026-09-20T10:00:00Z",
                "duration_seconds": 60,
            },
            {
                "id": "s2",
                "title": "فكرة ثانية",
                "published_at": "2026-09-18T10:00:00Z",
                "duration_seconds": 70,
            },
            {
                "id": "s3",
                "title": "فكرة ثالثة",
                "published_at": "2026-09-16T10:00:00Z",
                "duration_seconds": 55,
            },
        ]

    def test_refresh_collects_compact_metrics_and_builds_observational_signal(self) -> None:
        state = {"sessions": {}, "requests": {}}

        def fetch_live(_key: str) -> dict:
            return {
                "channel_id": "channel-1",
                "channel_title": "نداء اليقظة",
                "videos": self._videos(),
            }

        def post_form(_url: str, _data: dict[str, str], *, timeout: int) -> dict:
            self.assertGreater(timeout, 0)
            return {"access_token": "access-token"}

        def get_json(_url: str, *, params: dict, access_token: str, timeout: int) -> dict:
            self.assertEqual(access_token, "access-token")
            self.assertGreater(timeout, 0)
            video_id = str(params["filters"]).split("video==", 1)[1]
            if params.get("dimensions") == "elapsedVideoTimeRatio":
                return _retention_report(video_id)
            return _analytics_report(video_id)

        result = refresh_state(
            state,
            youtube_api_key="yt-key",
            client_id="client",
            client_secret="secret",
            refresh_token="refresh",
            now=datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc),
            fetch_live=fetch_live,
            post_form=post_form,
            get_json=get_json,
        )
        self.assertEqual(result["status"], "success")
        root = state[STATE_KEY]
        self.assertEqual(len(root["samples"]), 3)
        self.assertEqual(root["insights"]["mode"], "observational_only")
        self.assertFalse(root["insights"]["causal_claims"])
        short = root["insights"]["formats"][0]
        self.assertEqual(short["format"], "short")
        self.assertEqual(short["sample_count"], 3)
        self.assertEqual(short["median_avg_percentage_viewed"], 72.0)
        self.assertEqual(short["recurring_largest_drop_zone"], "opening")
        self.assertEqual(short["recurring_drop_support"], 3)
        memo = learning_memo(state, "short")
        self.assertIn("own-channel observational evidence", memo)
        self.assertIn("repeated largest-drop zone=opening", memo)
        line = learning_evidence_line(state, "short")
        self.assertTrue(line.startswith("[Channel learning]"))
        self.assertLessEqual(len(line), 240)

    def test_topic_performance_signal_steers_research_from_strong_family_without_copying(self) -> None:
        state = {
            STATE_KEY: {
                "samples": [
                    {
                        "video_id": "burnout",
                        "title": "الاحتراق الوظيفي",
                        "format": "short",
                        "views": 500,
                        "published_at": "2026-09-29T08:00:00Z",
                        "observed_at": "2026-09-30T08:00:00Z",
                    },
                    {
                        "video_id": "anxiety",
                        "title": "القلق",
                        "format": "short",
                        "views": 1,
                        "published_at": "2026-09-29T08:00:00Z",
                        "observed_at": "2026-09-30T08:00:00Z",
                    },
                ],
                "insights": {"formats": []},
            }
        }
        memo = learning_memo(state, "short")
        self.assertIn("TOPIC_PERFORMANCE_SIGNAL", memo)
        self.assertIn("الاحتراق الوظيفي", memo)
        self.assertIn("القلق", memo)
        self.assertIn("adjacent specific real-life problems", memo)
        self.assertIn("Do not copy it", memo)
        self.assertIn("Downrank broad abstract themes", memo)

    def test_fewer_than_three_samples_never_create_actionable_format_learning(self) -> None:
        report = build_insights(
            [
                {"video_id": "1", "format": "short", "avg_percentage_viewed": 70},
                {"video_id": "2", "format": "short", "avg_percentage_viewed": 75},
            ]
        )
        self.assertEqual(report["formats"], [])

    def test_missing_credentials_are_fail_open(self) -> None:
        state = {}
        result = refresh_state(
            state,
            youtube_api_key="",
            client_id="",
            client_secret="",
            refresh_token="",
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(state[STATE_KEY]["reason"], "missing_credentials")

    def test_failed_refresh_is_fail_open_and_throttled(self) -> None:
        state = {}

        def fail_post(*_args, **_kwargs):
            raise RuntimeError("temporary")

        first = refresh_state(
            state,
            youtube_api_key="yt",
            client_id="client",
            client_secret="secret",
            refresh_token="refresh",
            now=datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc),
            post_form=fail_post,
        )
        self.assertEqual(first["status"], "error")
        second = refresh_state(
            state,
            youtube_api_key="yt",
            client_id="client",
            client_secret="secret",
            refresh_token="refresh",
            now=datetime(2026, 9, 30, 1, 0, tzinfo=timezone.utc),
            post_form=fail_post,
        )
        self.assertEqual(second["status"], "skipped_fresh")
        self.assertFalse(second["changed"])

    def test_research_preserves_channel_learning_before_tavily_and_generic_grounding(self) -> None:
        source = (ROOT / "scripts" / "telegram_topic_research_v2_core.py").read_text(encoding="utf-8")
        self.assertIn("channel_learning_memo = learning_memo(learning_state, kind)", source)
        self.assertIn("channel_learning_memo,", source)
        self.assertIn("tavily_memo[:2600]", source)
        self.assertIn("existing_grounded[:2400]", source)
        self.assertIn('candidate["evidence"] = [channel_learning_line, *evidence][:6]', source)
        self.assertIn('"youtube_learning_applied": bool(channel_learning_line)', source)

    def test_planning_treats_channel_learning_as_noncausal_structure_evidence(self) -> None:
        pipeline = (ROOT / "clean_v2" / "pipeline.py").read_text(encoding="utf-8")
        self.assertIn("[Channel learning]", pipeline)
        self.assertIn("own-channel", pipeline)
        self.assertIn("not causal proof", pipeline)
        self.assertIn("automatic production override", pipeline)

    def test_oauth_stays_in_daily_snapshot_and_research_reads_only_persisted_learning(self) -> None:
        editorial = (ROOT / ".github" / "workflows" / "telegram-editorial-control.yml").read_text(encoding="utf-8")
        snapshot = (ROOT / ".github" / "workflows" / "clean-v2-youtube-snapshot.yml").read_text(encoding="utf-8")
        self.assertNotIn("YOUTUBE_CLIENT_ID:", editorial)
        self.assertNotIn("YOUTUBE_CLIENT_SECRET:", editorial)
        self.assertNotIn("YOUTUBE_REFRESH_TOKEN:", editorial)
        self.assertIn("Restore read-only YouTube learning for research", editorial)
        self.assertIn("YOUTUBE_LEARNING_STATE_PATH=", editorial)
        self.assertIn("clean-v2-telegram-state", editorial)
        self.assertIn("Refresh compact YouTube learning", snapshot)
        self.assertIn("python scripts/youtube_learning_lite.py refresh --state", snapshot)
        self.assertIn("YOUTUBE_CLIENT_ID: ${{ secrets.YOUTUBE_CLIENT_ID }}", snapshot)
        self.assertIn("YOUTUBE_CLIENT_SECRET: ${{ secrets.YOUTUBE_CLIENT_SECRET }}", snapshot)
        self.assertIn("YOUTUBE_REFRESH_TOKEN: ${{ secrets.YOUTUBE_REFRESH_TOKEN }}", snapshot)


if __name__ == "__main__":
    unittest.main()
