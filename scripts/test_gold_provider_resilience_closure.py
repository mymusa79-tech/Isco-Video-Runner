from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from isco_video_agent.ai_budget import (
    AttemptOutcome,
    BudgetLedger,
    Capability,
    Priority,
    TaskSpec,
)

from scripts import provider_health_registry as health
from scripts import vision_stage_contract_v2 as vision_contract
from scripts.gold_enforce_phase4 import _persist_budget_snapshot
from scripts.vision_provider_failure_unification_v1 import (
    _classify_health_failure,
    _shared_http_classification,
    gold_over_capacity_cooldown_scope,
)


class GoldProviderResilienceClosureTests(unittest.TestCase):
    def test_shared_taxonomy_covers_cloudflare_style_5xx_family(self) -> None:
        for status in (520, 521, 522, 523, 524, 599):
            with self.subTest(status=status):
                self.assertIs(
                    _shared_http_classification(status, "upstream unavailable"),
                    vision_contract.VisionErrorCode.PROVIDER_TRANSIENT,
                )

    def test_unknown_non_provider_http_shape_stays_unclassified(self) -> None:
        self.assertIsNone(_shared_http_classification(418, "teapot"))

    def test_runtime_model_over_capacity_uses_cooldown_only_inside_gold(self) -> None:
        ordinary_fallback = Mock(return_value=health.FAILURE_TRANSIENT)
        ordinary = _classify_health_failure(
            "HTTP_503 model currently over capacity",
            source="vision_stage",
            fallback=ordinary_fallback,
        )
        self.assertEqual(ordinary, health.FAILURE_TRANSIENT)
        ordinary_fallback.assert_called_once()

        gold_fallback = Mock(return_value=health.FAILURE_TRANSIENT)
        with gold_over_capacity_cooldown_scope():
            gold = _classify_health_failure(
                "HTTP_503 model currently over capacity",
                source="vision_stage",
                fallback=gold_fallback,
            )
        self.assertEqual(gold, health.FAILURE_RATE_LIMITED)
        gold_fallback.assert_not_called()

    def test_provider_preflight_remains_authoritative_even_inside_gold(self) -> None:
        fallback = Mock(return_value=health.FAILURE_HARD)
        with gold_over_capacity_cooldown_scope():
            result = _classify_health_failure(
                "HTTP_503 model currently over capacity",
                source="provider_preflight",
                fallback=fallback,
            )
        self.assertEqual(result, health.FAILURE_HARD)
        fallback.assert_called_once()

    def test_atomic_budget_snapshot_exists_before_and_after_attempt(self) -> None:
        ledger = BudgetLedger("story", enforce=True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = _persist_budget_snapshot(ledger, root)
            self.assertEqual(target, root / "ai-budget.json")
            self.assertTrue(target.is_file())
            self.assertFalse((root / ".ai-budget.json.tmp").exists())
            before = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(before["provider_attempts"]["total"], 0)

            spec = TaskSpec(
                task_id="GOLD_TEST_VISION",
                kind="VISUAL_AUDIT",
                priority=Priority.P0,
                capability=Capability.VISION,
                max_provider_attempts=2,
                schema_repair_allowed=False,
                local_fallback=False,
                semantic_block_is_final=True,
            )
            ledger.register_task(spec)
            ledger.record_attempt(
                spec.task_id,
                provider="groq",
                requested_model="qwen/test",
                resolved_model="qwen/test",
                capability=Capability.VISION,
                outcome=AttemptOutcome.RATE_LIMITED,
                detail="HTTP_503 model currently over capacity",
            )
            _persist_budget_snapshot(ledger, root)

            after = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(after["provider_attempts"]["total"], 1)
            self.assertEqual(after["provider_attempts"]["by_provider"], {"groq": 1})
            self.assertFalse((root / ".ai-budget.json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
