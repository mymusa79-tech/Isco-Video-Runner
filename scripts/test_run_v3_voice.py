from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.run_v3_voice as run_v3_voice


class ResolvePlanSourceTests(unittest.TestCase):
    """Covers item 1: plan_source must always name the real planner that produced a
    run's plan (gemini | groq | openrouter | product_proof_fallback), and must never
    claim a live provider succeeded when the fallback actually produced the plan."""

    def test_fallback_wins_even_if_providers_were_also_recorded(self) -> None:
        with patch.object(run_v3_voice, "was_fallback_used", return_value=True), \
                patch.object(run_v3_voice, "get_used_providers", return_value=["gemini"]):
            self.assertEqual(run_v3_voice._resolve_plan_source(), "product_proof_fallback")

    def test_single_provider(self) -> None:
        with patch.object(run_v3_voice, "was_fallback_used", return_value=False), \
                patch.object(run_v3_voice, "get_used_providers", return_value=["gemini"]):
            self.assertEqual(run_v3_voice._resolve_plan_source(), "gemini")

    def test_multiple_providers_are_joined(self) -> None:
        with patch.object(run_v3_voice, "was_fallback_used", return_value=False), \
                patch.object(run_v3_voice, "get_used_providers", return_value=["gemini", "groq"]):
            self.assertEqual(run_v3_voice._resolve_plan_source(), "gemini+groq")

    def test_no_providers_recorded_is_unknown_not_a_false_claim(self) -> None:
        with patch.object(run_v3_voice, "was_fallback_used", return_value=False), \
                patch.object(run_v3_voice, "get_used_providers", return_value=[]):
            self.assertEqual(run_v3_voice._resolve_plan_source(), "unknown")


class TagPlanSourceTests(unittest.TestCase):
    """Covers item 1: plan_source must land in every JSON artifact the workflow
    uploads (plan.json, quality-final.json), not just one of them."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_tags_both_plan_and_quality_json(self) -> None:
        (self.out_dir / "plan.json").write_text(json.dumps({"topic": "x"}), encoding="utf-8")
        (self.out_dir / "quality-final.json").write_text(json.dumps({"duration_ok": True}), encoding="utf-8")

        with patch.object(run_v3_voice, "was_fallback_used", return_value=False), \
                patch.object(run_v3_voice, "get_used_providers", return_value=["groq"]):
            run_v3_voice._tag_plan_source(self.out_dir)

        plan = json.loads((self.out_dir / "plan.json").read_text(encoding="utf-8"))
        quality = json.loads((self.out_dir / "quality-final.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["plan_source"], "groq")
        self.assertEqual(quality["plan_source"], "groq")
        # Original fields must survive the round-trip untouched.
        self.assertEqual(plan["topic"], "x")
        self.assertTrue(quality["duration_ok"])

    def test_missing_artifact_is_skipped_not_an_error(self) -> None:
        (self.out_dir / "plan.json").write_text(json.dumps({"topic": "x"}), encoding="utf-8")
        # quality-final.json deliberately absent.

        with patch.object(run_v3_voice, "was_fallback_used", return_value=True), \
                patch.object(run_v3_voice, "get_used_providers", return_value=[]):
            run_v3_voice._tag_plan_source(self.out_dir)  # must not raise

        plan = json.loads((self.out_dir / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["plan_source"], "product_proof_fallback")
        self.assertFalse((self.out_dir / "quality-final.json").exists())


if __name__ == "__main__":
    unittest.main()
