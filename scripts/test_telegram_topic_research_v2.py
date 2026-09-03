from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts import telegram_topic_research_v2 as v2
from scripts.workflow_hygiene import _canonical_engine_pin


ROOT = Path(__file__).resolve().parents[1]


class TelegramTopicResearchV2Tests(unittest.TestCase):
    def _candidate(self, title: str, trend: float, evergreen: float, score: float) -> dict:
        return {
            "title": title,
            "market_timing_status": "measured",
            "source_mode": "live_research",
            "trend_score": trend,
            "evergreen_score": evergreen,
            "channel_fit_score": 0.88,
            "opportunity_score": 0.84,
            "evidence_quality": 0.75,
            "production_feasibility": 0.90,
            "hook_potential": 0.90,
            "retention_potential": 0.88,
            "emotional_pull": 0.82,
            "audience_fit": 0.91,
            "title_thumbnail_potential": 0.89,
            "control_score": score,
            "market_query": title,
            "market_evidence": [
                {
                    "source_type": "youtube_recent_search",
                    "query": title,
                    "window_days": 30,
                    "sample_count": 4,
                    "distinct_channels": 3,
                    "max_views_per_day": 2200.0,
                    "median_views_per_day": 900.0,
                    "fetched_at": "2026-08-30T18:00:00Z",
                    "top_samples": [],
                }
            ],
        }

    def test_recent_seen_topics_blocks_recent_displayed_sessions(self):
        state = {
            "sessions": {
                "old": {
                    "kind": "long",
                    "created_at": "2026-07-01T00:00:00Z",
                    "candidates": [{"title": "قديم"}],
                },
                "new": {
                    "kind": "long",
                    "created_at": "2026-08-25T00:00:00Z",
                    "candidates": [{"title": "حديث"}, {"title": "حديث آخر"}],
                },
            }
        }
        seen = v2._recent_seen_topics(
            state,
            "long",
            now=datetime(2026, 8, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(seen, ["حديث", "حديث آخر"])

    def test_short_and_long_seen_cooldowns_are_independent(self):
        state = {
            "sessions": {
                "long": {
                    "kind": "long",
                    "created_at": "2026-08-29T00:00:00Z",
                    "candidates": [{"title": "حلقة حديثة"}],
                },
                "short": {
                    "kind": "short",
                    "created_at": "2026-08-29T00:00:00Z",
                    "candidates": [{"title": "شورت حديث"}],
                },
            }
        }
        now = datetime(2026, 8, 30, tzinfo=timezone.utc)
        self.assertEqual(v2._recent_seen_topics(state, "short", now=now), ["شورت حديث"])
        self.assertEqual(v2._recent_seen_topics(state, "long", now=now), ["حلقة حديثة"])

    def test_diverse_top_prefers_different_market_classes(self):
        candidates = [
            self._candidate("صاعد", 0.80, 0.60, 0.92),
            self._candidate("هجين", 0.55, 0.90, 0.90),
            self._candidate("دائم", 0.30, 0.92, 0.88),
            self._candidate("صاعد ثان", 0.75, 0.65, 0.91),
        ]
        chosen = v2._diverse_top(candidates, 3)
        self.assertEqual([v2._market_class(item) for item in chosen], ["rising", "hybrid", "evergreen"])

    def test_detail_exposes_market_provenance(self):
        candidate = self._candidate("موضوع", 0.72, 0.88, 0.90)
        text = v2._candidate_detail(candidate, 0)
        self.assertIn("الاهتمام الحالي المقاس", text)
        self.assertIn("آخر 30 يومًا", text)
        self.assertIn("3 قنوات", text)
        self.assertIn("مشاهدة/يوم", text)
        self.assertIn("ليست ادعاءً بأنها قراءة مباشرة", text)

    def test_fallback_is_not_accepted_as_live_market_candidate(self):
        candidate = self._candidate("موضوع", 0.70, 0.85, 0.88)
        candidate["source_mode"] = "fallback_library"
        live = [
            item
            for item in [candidate]
            if item.get("market_timing_status") == "measured" and item.get("source_mode") == "live_research"
        ]
        self.assertEqual(live, [])

    def test_short_payload_uses_same_live_contract_and_adds_short_admission(self):
        candidate = self._candidate("فكرة شورت", 0.72, 0.75, 0.90)
        payload = v2._build_candidate_payload(candidate, "short")
        self.assertEqual(payload["research_contract_version"], v2.RESEARCH_CONTRACT_VERSION)
        self.assertEqual(payload["source_mode"], "live_research")
        self.assertEqual(payload["market_timing_status"], "measured")
        self.assertEqual(payload["format_hint"], "moment")
        self.assertIn("short_admission", payload)
        self.assertIn("single_action_contract", payload["short_admission"])
        self.assertGreater(payload["short_admission"]["short_fit_score"], 0)

    def test_short_score_keeps_measured_market_timing_in_formula(self):
        low_timing = self._candidate("شورت أ", 0.10, 0.75, 0.0)
        high_timing = self._candidate("شورت ب", 0.90, 0.75, 0.0)
        self.assertGreater(v2._control_score(high_timing, "short"), v2._control_score(low_timing, "short"))

    def test_short_panel_is_explicitly_live_research(self):
        candidate = v2._build_candidate_payload(self._candidate("فكرة شورت", 0.72, 0.75, 0.90), "short")
        text = v2._candidate_panel_text("short", [candidate, candidate, candidate])
        self.assertIn("3 فرص بحث حي للشورت", text)
        self.assertIn("الآن:", text)
        self.assertIn("ملاءمة القناة", text)

    def test_workflow_separates_research_engine_from_production_engine(self):
        workflow_dir = ROOT / ".github/workflows"
        production_engine_sha = _canonical_engine_pin(workflow_dir)
        self.assertIsNotNone(production_engine_sha)
        assert production_engine_sha is not None

        workflow = (workflow_dir / "telegram-editorial-control.yml").read_text(encoding="utf-8")
        research_engine_sha = "bf85607f6e34dcedc199abad7e610b12c4685309"
        self.assertIn(f"ENGINE_SHA: {production_engine_sha}", workflow)
        self.assertIn(f"RESEARCH_ENGINE_SHA: {research_engine_sha}", workflow)
        self.assertNotEqual(production_engine_sha, research_engine_sha)
        self.assertIn("ref: ${{ env.RESEARCH_ENGINE_SHA }}", workflow)
        self.assertIn('python scripts/telegram_topic_research_v2.py research --state "$CONTROL_STATE_PATH"', workflow)
        self.assertIn('-f engine_sha="$ENGINE_SHA"', workflow)


if __name__ == "__main__":
    unittest.main()
