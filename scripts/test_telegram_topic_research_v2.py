from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import telegram_topic_research_v2 as v2
from scripts import topic_research_ranking_policy as ranking
from scripts.workflow_hygiene import _canonical_engine_pin


ROOT = Path(__file__).resolve().parents[1]


class TelegramTopicResearchV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ranking.install(core=v2.core, panel=v2.core.panel)

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
            "competition_opportunity": 0.72,
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

    def test_research_contract_targets_three_but_accepts_one(self):
        self.assertEqual(v2.TARGET_RESEARCH_OPTIONS, 3)
        self.assertEqual(v2.MIN_LIVE_MARKET_CANDIDATES, 1)

    def test_market_thresholds_are_explicit_and_do_not_call_sub_65_strong_current(self):
        self.assertEqual(v2.STRONG_CURRENT_INTEREST_MIN, 0.65)
        self.assertEqual(v2.HYBRID_CURRENT_INTEREST_MIN, 0.50)
        self.assertEqual(v2.EVERGREEN_STRENGTH_MIN, 0.70)
        self.assertEqual(v2._market_class(self._candidate("64", 0.64, 0.50, 0.9)), "explore")
        self.assertEqual(v2._market_class(self._candidate("65", 0.65, 0.50, 0.9)), "rising")
        self.assertEqual(v2._market_class(self._candidate("49 evergreen", 0.49, 0.90, 0.9)), "evergreen")
        self.assertEqual(v2._market_class(self._candidate("50 hybrid", 0.50, 0.90, 0.9)), "hybrid")

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
            v2._build_candidate_payload(self._candidate("صاعد", 0.80, 0.60, 0.92), "long"),
            v2._build_candidate_payload(self._candidate("هجين", 0.55, 0.90, 0.90), "long"),
            v2._build_candidate_payload(self._candidate("دائم", 0.30, 0.92, 0.88), "long"),
            v2._build_candidate_payload(self._candidate("صاعد ثان", 0.75, 0.65, 0.91), "long"),
        ]
        chosen = v2._diverse_top(candidates, 3)
        self.assertEqual([v2._market_class(item) for item in chosen], ["rising", "hybrid", "evergreen"])

    def test_market_admission_keeps_weak_explore_out_of_primary_slots(self):
        weak = v2._build_candidate_payload(self._candidate("ضعيف", 0.49, 0.60, 0.99), "long")
        evergreen = v2._build_candidate_payload(self._candidate("دائم", 0.42, 0.90, 0.70), "long")
        chosen = v2._diverse_top([weak, evergreen], 3)
        self.assertEqual([item["title"] for item in chosen], ["دائم"])
        self.assertEqual(v2._market_class(weak), "explore")

    def test_current_market_role_is_selected_before_high_composite_evergreen(self):
        current = v2._build_candidate_payload(self._candidate("الآن", 0.66, 0.60, 0.10), "long")
        evergreen = v2._build_candidate_payload(self._candidate("دائم", 0.30, 0.95, 0.99), "long")
        evergreen["control_score"] = 0.99
        current["control_score"] = 0.60
        chosen = v2._diverse_top([evergreen, current], 2)
        self.assertEqual(chosen[0]["title"], "الآن")
        self.assertEqual(v2._market_class(chosen[0]), "rising")

    def test_payload_exposes_separate_market_channel_creative_execution_components(self):
        payload = v2._build_candidate_payload(self._candidate("متعدد", 0.72, 0.80, 0.90), "long")
        self.assertEqual(set(payload["ranking_components"]), {"market", "channel", "creative", "execution"})
        self.assertEqual(payload["ranking_components"]["market"], 0.72)
        self.assertGreater(payload["ranking_components"]["channel"], 0.0)
        self.assertGreater(payload["ranking_components"]["creative"], 0.0)
        self.assertGreater(payload["ranking_components"]["execution"], 0.0)

    def test_detail_exposes_market_provenance_and_multistage_scores(self):
        candidate = v2._build_candidate_payload(self._candidate("موضوع", 0.72, 0.88, 0.90), "long")
        text = v2._candidate_detail(candidate, 0)
        self.assertIn("الاهتمام الحالي المقاس", text)
        self.assertIn("القوة الإبداعية", text)
        self.assertIn("ثقة التنفيذ", text)
        self.assertIn("آخر 30 يومًا", text)
        self.assertIn("3 قنوات", text)
        self.assertIn("مشاهدة/يوم", text)
        self.assertIn("التصنيف السوقي مستقل", text)

    def test_fallback_is_not_accepted_as_live_market_candidate(self):
        candidate = self._candidate("موضوع", 0.70, 0.85, 0.88)
        candidate["source_mode"] = "fallback_library"
        live = [
            item
            for item in [candidate]
            if item.get("market_timing_status") == "measured" and item.get("source_mode") == "live_research"
        ]
        self.assertEqual(live, [])
        self.assertEqual(v2._market_class(candidate), "unverified")

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

    def test_full_panel_still_shows_three_when_three_are_valid(self):
        candidates = [
            v2._build_candidate_payload(self._candidate(f"فكرة {i}", 0.72, 0.75, 0.90 - i * 0.01), "short")
            for i in range(3)
        ]
        text = v2._candidate_panel_text("short", candidates)
        self.assertIn("3 فرص بحث حي للشورت", text)
        self.assertNotIn("لم أخفّض", text)
        self.assertNotIn("لم توجد فرصة «قوية الآن»", text)

    def test_panel_is_honest_when_only_evergreen_has_value(self):
        candidate = v2._build_candidate_payload(
            self._candidate("دائم", 0.45, 0.90, 0.90), "long"
        )
        text = v2._candidate_panel_text("long", [candidate])
        self.assertIn("لم توجد فرصة «قوية الآن»", text)
        self.assertIn("Evergreen قوي — ليس فرصة حالية", text)
        self.assertIn("الآن: 4.5/10", text)

    def test_partial_panel_is_honest_and_does_not_claim_three(self):
        candidate = v2._build_candidate_payload(
            self._candidate("فكرة شورت", 0.72, 0.75, 0.90), "short"
        )
        text = v2._candidate_panel_text("short", [candidate])
        self.assertIn("1 فرصة بحث حي للشورت", text)
        self.assertIn("وجدت 1 خيارًا صالحًا فقط", text)
        self.assertIn("لم أخفّض أي Quality/Market Gate", text)

    def test_partial_keyboard_removes_impossible_pick_buttons(self):
        base_rows = [
            [{"text": "one"}],
            [{"text": "two"}],
            [{"text": "three"}],
            [{"text": "refresh"}],
            [{"text": "home"}],
        ]
        with patch.object(v2.panel, "_candidate_keyboard", return_value=base_rows):
            rows = v2._candidate_keyboard("session", "short", 1)
        self.assertEqual(rows, [base_rows[0], base_rows[3], base_rows[4]])

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

    def test_topic_research_gate_owns_the_real_core_module_end_to_end(self):
        workflow = (ROOT / ".github/workflows/verify-topic-research-v2.yml").read_text(encoding="utf-8")
        self.assertIn('- "scripts/telegram_topic_research_v2_core.py"', workflow)
        self.assertIn('- "scripts/topic_research_ranking_policy.py"', workflow)
        self.assertIn("scripts/telegram_topic_research_v2_core.py", workflow)
        self.assertIn("scripts/topic_research_ranking_policy.py", workflow)
        self.assertIn("scripts/test_telegram_topic_research_v2.py", workflow)


if __name__ == "__main__":
    unittest.main()
