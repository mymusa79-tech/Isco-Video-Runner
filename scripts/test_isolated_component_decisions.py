from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class IsolatedComponentDecisionTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_gold_phase4_is_live_without_parallel_legacy_stages(self) -> None:
        production = self.read("scripts/run_v3_voice.py")
        phase4 = self.read("scripts/gold_enforce_phase4.py")

        self.assertIn("run_gold_enforce_phase4", production)
        self.assertNotIn("gold_shadow_phase2b", production)
        self.assertNotIn("gold_single_evaluator_phase3", production)
        self.assertIn("from scripts.gold_shadow_phase2a import", phase4)
        self.assertIn("_fingerprint", phase4)
        self.assertIn("_provider_attempt_total", phase4)

    def test_retention_v2_remains_outside_production_entrypoint(self) -> None:
        production = self.read("scripts/run_v3_voice.py")
        for token in (
            "retention_signals",
            "retention_cohorts",
            "retention_reach",
            "retention_experiments",
            "retention_influence",
        ):
            self.assertNotIn(token, production)

        decision = self.read("docs/RETENTION_V2_ACTIVATION_RUNBOOK_2026-08-28.md")
        self.assertIn("post-publication", decision)
        self.assertIn("deferred", decision)
        self.assertIn("durable retention evidence", decision)
        self.assertIn("disabled` or `review_only", decision)

    def test_local_brain_has_one_manual_canonical_benchmark(self) -> None:
        canonical_path = ROOT / ".github/workflows/local-brain-benchmark.yml"
        self.assertTrue(canonical_path.is_file())
        self.assertFalse((ROOT / ".github/workflows/local-brain-benchmark-v2.yml").exists())
        self.assertFalse((ROOT / ".github/workflows/local-brain-benchmark-v3.yml").exists())

        workflow = canonical_path.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("\n  push:", workflow)
        self.assertIn("LLAMA_TAG: b9637", workflow)
        self.assertIn("7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5", workflow)
        self.assertIn("LD_LIBRARY_PATH=$RUNNER_TEMP/isco-local-brain/runtime", workflow)
        self.assertIn("libllama-server-impl.so", workflow)
        self.assertIn('cp -a "$SERVER_DIR"/. "$MODEL_DIR/runtime"/', workflow)

        production = self.read("scripts/run_v3_voice.py")
        self.assertNotIn("local_brain", production.lower())

    def test_decision_documents_use_non_live_statuses(self) -> None:
        gold = self.read("docs/GOLD_PHASE2A_2B_3_DECISION_2026-08-28.md")
        local = self.read("docs/LOCAL_BRAIN_DECISION_2026-08-28.md")
        self.assertIn("successfully superseded migration/shadow stages", gold)
        self.assertIn("Phase 4 = live and authoritative", gold)
        self.assertIn("not a live production fallback", local)
        self.assertIn("manual-only", local)


if __name__ == "__main__":
    unittest.main()
