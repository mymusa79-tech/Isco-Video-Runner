from __future__ import annotations

import unittest
from pathlib import Path

from scripts.audience_reality_signals import (
    PROMPT_SUFFIX,
    attach_signals,
    augment_research_prompt,
    detail_lines,
    extract_signals,
)


ROOT = Path(__file__).resolve().parents[1]


class AudienceRealitySignalsTests(unittest.TestCase):
    def test_prompt_augmentation_is_idempotent_and_zero_extra_access(self) -> None:
        prompt = "research prompt"
        once = augment_research_prompt(prompt)
        twice = augment_research_prompt(once)
        self.assertEqual(once, twice)
        self.assertIn("AUDIENCE REALITY SIGNALS", once)
        self.assertIn("Do not browse, scrape", PROMPT_SUFFIX)
        self.assertNotIn("reddit.com", once.casefold())
        self.assertNotIn("api.reddit", once.casefold())

    def test_extracts_bounded_audience_and_approved_reddit_signals(self) -> None:
        signals = extract_signals(
            [
                "[Audience pain] Keeps checking work messages late at night",
                "[Audience situation] Sitting in bed reopening the same inbox",
                "[Audience question] Why do I feel behind even when I am working?",
                "[Audience visual] hands switching between calendar and unread messages",
                "[Reddit visual] commuter staring at a job email on a phone",
                "[Audience visual] hands switching between calendar and unread messages",
            ]
        )
        self.assertEqual(signals["pain"][0]["source"], "audience")
        self.assertEqual(signals["visual"][1]["source"], "reddit")
        self.assertEqual(len(signals["visual"]), 2)

    def test_candidate_payload_attachment_and_detail_are_compact(self) -> None:
        candidate = attach_signals(
            {
                "evidence": [
                    "[Audience pain] Rechecking messages instead of sleeping",
                    "[Audience visual] phone glow over hands in a dark room",
                ]
            }
        )
        self.assertIn("community_signals", candidate)
        rendered = detail_lines(candidate)
        self.assertEqual(len(rendered), 2)
        self.assertTrue(any("المشهد" in line for line in rendered))

    def test_research_provider_reuses_existing_call_instead_of_new_reddit_provider(self) -> None:
        source = (ROOT / "scripts" / "research_provider_reliability.py").read_text(encoding="utf-8")
        self.assertIn("prompt = augment_research_prompt(prompt)", source)
        self.assertNotIn("reddit.com", source.casefold())
        self.assertNotIn("api.reddit", source.casefold())

    def test_both_topic_payload_layers_preserve_signals(self) -> None:
        core = (ROOT / "scripts" / "telegram_topic_research_v2_core.py").read_text(encoding="utf-8")
        ranking = (ROOT / "scripts" / "topic_research_ranking_policy.py").read_text(encoding="utf-8")
        self.assertIn("normalized = attach_signals(candidate)", core)
        self.assertIn("normalized = attach_signals(candidate)", ranking)
        self.assertIn("إشارات واقعية تساعد الكاتب والفيجوال", core)
        self.assertIn("إشارات واقعية تساعد الكاتب والفيجوال", ranking)

    def test_planning_consumes_signals_for_hook_and_visual_story(self) -> None:
        pipeline = (ROOT / "clean_v2" / "pipeline.py").read_text(encoding="utf-8")
        self.assertIn("[Audience pain]", pipeline)
        self.assertIn("[Audience visual]", pipeline)
        self.assertIn("visual_story shot_intent and stock_query_en", pipeline)
        self.assertIn("[Reddit ...] line, if an approved external source supplied one", pipeline)


if __name__ == "__main__":
    unittest.main()
