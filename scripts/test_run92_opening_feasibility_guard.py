from __future__ import annotations

import unittest
from unittest.mock import patch

import isco_video_agent.opening_director as opening_director
from isco_video_agent.visual_selection import VisualCandidateCache

from scripts.opening_feasibility_guard import (
    STOCK_CANDIDATE_POOL,
    _adaptive_review_cap,
    _install_stock_search_wrappers,
    _preserve_outline_visual_intent,
    _stable_intent_audit,
    adaptive_opening_slot_specs,
    stock_safe_search_query,
)


def _candidate(asset_id: int, seconds: float) -> dict:
    return {
        "id": asset_id,
        "duration": seconds,
        "video_files": [
            {
                "link": f"https://videos.pexels.com/video-{asset_id}.mp4",
                "width": 1920,
                "height": 1080,
            }
        ],
    }


class Run92OpeningFeasibilityGuardTests(unittest.TestCase):
    def test_run92_query_keeps_environment_semantics_for_search_only(self) -> None:
        original = "person sitting by wooden table in sunlit room looking pensively at empty notebook"
        self.assertEqual(
            stock_safe_search_query(original),
            "wooden table sunlit room empty notebook",
        )

    def test_safe_non_identifiable_framing_is_preserved(self) -> None:
        self.assertEqual(stock_safe_search_query("hands writing notebook"), "hands writing notebook")
        self.assertEqual(
            stock_safe_search_query("silhouette person walking road at sunset"),
            "silhouette person walking road at sunset",
        )

    def test_human_only_query_uses_broad_safe_fallback(self) -> None:
        self.assertEqual(
            stock_safe_search_query("person sitting alone thinking"),
            "quiet room natural light",
        )

    def test_outline_keeps_original_visual_intent_and_adds_search_derivative(self) -> None:
        original = "person sitting by wooden table in sunlit room looking pensively at empty notebook"
        outline = {"section_briefs": [{"id": "s1", "visual_query": original}]}
        result = _preserve_outline_visual_intent(outline, fmt="film")
        self.assertEqual(result["section_briefs"][0]["visual_query"], original)
        self.assertEqual(
            result["section_briefs"][0]["stock_search_query"],
            "wooden table sunlit room empty notebook",
        )

    def test_run92_duration_becomes_fixed_30s_opening_plus_stock_friendly_tail(self) -> None:
        specs = adaptive_opening_slot_specs(62.3)
        self.assertEqual([slot.key for slot in specs], ["cold_open", "escalation", "promise", "body_1"])
        self.assertAlmostEqual(sum(slot.seconds for slot in specs), 62.3)
        self.assertEqual([slot.seconds for slot in specs[:3]], [7.0, 11.0, 12.0])
        self.assertAlmostEqual(specs[3].seconds, 32.3)
        self.assertTrue(all(slot.seconds <= 45.0 for slot in specs))

    def test_long_first_section_tail_is_split_without_looping_requirement(self) -> None:
        specs = adaptive_opening_slot_specs(100.0)
        self.assertEqual([slot.key for slot in specs], ["cold_open", "escalation", "promise", "body_1", "body_2"])
        self.assertAlmostEqual(sum(slot.seconds for slot in specs), 100.0)
        self.assertAlmostEqual(specs[3].seconds, 35.0)
        self.assertAlmostEqual(specs[4].seconds, 35.0)
        self.assertTrue(all(slot.seconds <= 45.0 for slot in specs))

    def test_135s_engine_ceiling_remains_bounded(self) -> None:
        specs = adaptive_opening_slot_specs(135.0)
        self.assertEqual(len(specs), 6)
        self.assertAlmostEqual(sum(slot.seconds for slot in specs), 135.0)
        self.assertTrue(all(slot.seconds <= 45.0 for slot in specs))
        self.assertEqual(_adaptive_review_cap(135.0), 7)

    def test_short_first_section_still_uses_legacy_path(self) -> None:
        self.assertEqual(adaptive_opening_slot_specs(25.0), [])

    def test_run92_geometry_can_select_four_distinct_real_assets(self) -> None:
        calls: list[int] = []

        def audit_fn(**kwargs):
            calls.append(int(kwargs["candidate"]["id"]))
            return {"status": "pass", "relevance": 0.95, "visual_quality": 0.95}

        with patch.object(opening_director, "opening_slot_specs", adaptive_opening_slot_specs):
            result = opening_director.select_opening_sequence(
                {
                    "pexels": [
                        _candidate(1, 40),
                        _candidate(2, 20),
                        _candidate(3, 15),
                        _candidate(4, 10),
                        _candidate(5, 8),
                    ]
                },
                section_seconds=62.3,
                portrait=False,
                narration_context="opening narration",
                intended_visual="original visual intent",
                audit_fn=audit_fn,
                cache=VisualCandidateCache(excluded_assets={}),
                max_reviews=_adaptive_review_cap(62.3),
            )

        self.assertEqual(result.status, "selected")
        self.assertEqual(len(result.slots), 4)
        selected_ids = [slot.review.candidate["id"] for slot in result.slots]
        self.assertEqual(len(set(selected_ids)), 4)
        self.assertLessEqual(len(calls), 5)

    def test_alternate_search_never_redefines_vision_intent(self) -> None:
        seen_intents: list[str] = []
        original_intent = "person at a desk resisting outside pressure"

        def raw_audit(**kwargs):
            seen_intents.append(str(kwargs["intended_visual"]))
            return {"status": "pass", "relevance": 0.9, "visual_quality": 0.9}

        stable_audit = _stable_intent_audit(raw_audit, original_intent)

        with patch.object(opening_director, "opening_slot_specs", adaptive_opening_slot_specs):
            result = opening_director.select_opening_sequence(
                {"pexels": []},
                section_seconds=30.0,
                portrait=False,
                narration_context="opening narration",
                intended_visual=original_intent,
                audit_fn=stable_audit,
                cache=VisualCandidateCache(excluded_assets={}),
                alternate_query_fn=lambda: "empty desk notebook window",
                alternate_search_fn=lambda _query: {
                    "pexels": [_candidate(101, 20), _candidate(102, 15), _candidate(103, 10)]
                },
                max_reviews=4,
            )

        self.assertEqual(result.status, "selected")
        self.assertEqual(seen_intents, [original_intent, original_intent, original_intent])

    def test_stock_pool_wrapper_expands_only_core_12_result_request(self) -> None:
        import isco_video_agent.orchestrator as orchestrator

        calls: list[tuple[str, int]] = []

        def fake_pexels(_key, query, orientation="landscape", per_page=15):
            calls.append((query, per_page))
            return []

        with patch.object(orchestrator, "pexels_search_videos", fake_pexels):
            _install_stock_search_wrappers()
            orchestrator.pexels_search_videos(
                "key",
                "person sitting by wooden table in sunlit room looking pensively at empty notebook",
                orientation="landscape",
                per_page=12,
            )
            orchestrator.pexels_search_videos("key", "forest road", per_page=7)

        self.assertEqual(calls[0], ("wooden table sunlit room empty notebook", STOCK_CANDIDATE_POOL))
        self.assertEqual(calls[1], ("forest road", 7))


if __name__ == "__main__":
    unittest.main()
