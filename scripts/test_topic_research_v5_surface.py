from __future__ import annotations

import unittest
from pathlib import Path

from scripts import telegram_creator_control_center_v5 as creator_v5
from scripts import telegram_topic_research_v2_core as core
from scripts import topic_research_ranking_policy as ranking
from scripts import topic_research_v5_surface as surface


ROOT = Path(__file__).resolve().parents[1]


class _Client:
    def __init__(self):
        self.messages = []

    def send(self, chat_id, text, *, keyboard=None):
        self.messages.append((chat_id, text, keyboard))


class TopicResearchV5SurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reproduce the production ordering that matters for this contract. V5 is
        # allowed to install its normal navigation surface first; the ranking bridge
        # must then own the final candidate presentation without changing authority.
        core.install_v2()
        ranking.install(core=core, panel=core.panel)
        creator_v5.install(core)
        surface.install(core=core, panel=core.panel, creator_v5=creator_v5)

    @staticmethod
    def _candidate(title: str, trend: float, evergreen: float) -> dict:
        raw = {
            "title": title,
            "market_timing_status": "measured",
            "source_mode": "live_research",
            "trend_score": trend,
            "evergreen_score": evergreen,
            "audience_fit": 0.91,
            "emotional_pull": 0.84,
            "title_thumbnail_potential": 0.88,
            "retention_potential": 0.89,
            "competition_opportunity": 0.72,
            "hook_potential": 0.90,
            "evidence_quality": 0.78,
            "production_feasibility": 0.92,
            "market_query": title,
            "market_evidence": [
                {
                    "query": title,
                    "window_days": 30,
                    "sample_count": 4,
                    "distinct_channels": 3,
                    "max_views_per_day": 1800.0,
                    "median_views_per_day": 820.0,
                    "fetched_at": "2026-09-04T18:00:00Z",
                    "top_samples": [],
                }
            ],
        }
        return ranking._build_candidate_payload(raw, "long")

    def test_final_v5_card_never_calls_low_current_evergreen_best_now(self):
        candidate = self._candidate("موضوع دائم", 0.45, 0.90)
        text = creator_v5._candidate_panel_text("long", [candidate])
        self.assertIn("Evergreen قوي — ليس فرصة حالية", text)
        self.assertIn("لم توجد فرصة «قوية الآن»", text)
        self.assertIn("الآن: 4.5/10", text)
        self.assertNotIn("الأنسب الآن", text)
        self.assertNotIn("فرصة الآن: زخم حديث قوي", text)

    def test_final_v5_search_copy_promises_up_to_three_not_exactly_three(self):
        self.assertIn("حتى 3", creator_v5._search_text())
        started = creator_v5._research_started_text("topic")
        self.assertIn("حتى 3", started)
        self.assertIn("لن أصف Evergreen منخفض الزخم كأنه ترند", started)

    def test_ready_status_reports_actual_one_candidate_count(self):
        candidate = self._candidate("موضوع واحد", 0.70, 0.80)
        state = {
            "sessions": {
                "s1": {
                    "session_id": "s1",
                    "kind": "long",
                    "created_at": "2026-09-04T18:00:00Z",
                    "candidates": [candidate],
                }
            },
            core.active.ACTIVE_RESEARCH_SESSION_KEY: "s1",
            "pending_actions": [],
            "requests": {},
            "production_queue": [],
        }
        text, _ = creator_v5._operator_status(state, None)
        self.assertIn("خيار واحد جاهز للمراجعة", text)
        self.assertNotIn("3 خيارات جاهزة", text)

    def test_reopening_partial_session_has_no_impossible_pick_buttons(self):
        candidate = self._candidate("موضوع واحد", 0.70, 0.80)
        state = {
            "sessions": {
                "s1": {
                    "session_id": "s1",
                    "kind": "long",
                    "created_at": "2026-09-04T18:00:00Z",
                    "candidates": [candidate],
                }
            },
            "pending_actions": [],
            "requests": {},
            "production_queue": [],
        }
        client = _Client()
        core.panel._handle_command("choices-s1", client, state, None, 77)
        self.assertEqual(len(client.messages), 1)
        _, text, keyboard = client.messages[0]
        self.assertIn("1 فرصة بحث حي للحلقة", text)
        callbacks = [
            button.get("callback_data", "")
            for row in (keyboard or [])
            for button in row
        ]
        self.assertTrue(any(value.startswith("pick:s1:0") for value in callbacks))
        self.assertTrue(any(value.startswith("detail:s1:0") for value in callbacks))
        self.assertFalse(any(value.startswith("pick:s1:1") for value in callbacks))
        self.assertFalse(any(value.startswith("pick:s1:2") for value in callbacks))
        self.assertFalse(any(value.startswith("detail:s1:1") for value in callbacks))
        self.assertFalse(any(value.startswith("detail:s1:2") for value in callbacks))

    def test_surface_bridge_contains_no_production_dispatch_authority(self):
        source = (ROOT / "scripts/topic_research_v5_surface.py").read_text(encoding="utf-8")
        forbidden = (
            "workflow_dispatch",
            "enqueue_request(",
            "pending_dispatch",
            "production_dispatch_authorized =",
        )
        for token in forbidden:
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
