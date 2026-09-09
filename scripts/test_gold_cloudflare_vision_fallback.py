from __future__ import annotations

import inspect
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from isco_video_agent.ai_budget import BudgetLedger, Capability, Priority, TaskSpec

from scripts import gold_cloudflare_vision_fallback as cloudflare
from scripts import vision_stage_contract_v2 as contract


def _spec() -> TaskSpec:
    return TaskSpec(
        task_id=cloudflare.GOLD_OPENING_TASK_ID,
        kind="VISUAL_AUDIT",
        priority=Priority.P0,
        capability=Capability.VISION,
        max_provider_attempts=5,
        schema_repair_allowed=False,
        local_fallback=False,
        semantic_block_is_final=True,
    )


def _preview(root: str) -> Path:
    path = Path(root) / "opening-preview.mp4"
    path.write_bytes(b"preview")
    return path


class CloudflareGoldVisionZeroCostTests(unittest.TestCase):
    def setUp(self) -> None:
        cloudflare.reset_attempt_scope()

    def test_zero_cost_route_is_explicit_opt_in(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(cloudflare._enabled())
        with patch.dict(
            os.environ,
            {"CLOUDFLARE_GOLD_VISION_FREE_ONLY": "true"},
            clear=True,
        ):
            self.assertTrue(cloudflare._enabled())

    def test_active_paid_or_unknown_subscription_is_not_proven_free(self) -> None:
        self.assertFalse(
            cloudflare._subscription_is_proven_free(
                {
                    "state": "Paid",
                    "price": 5,
                    "rate_plan": {"id": "pro"},
                }
            )
        )
        self.assertFalse(
            cloudflare._subscription_is_proven_free(
                {
                    "state": "Provisioned",
                    "price": None,
                    "rate_plan": {"id": "unknown"},
                }
            )
        )
        self.assertTrue(
            cloudflare._subscription_is_proven_free(
                {
                    "state": "Provisioned",
                    "price": 0,
                    "rate_plan": {"id": "free"},
                }
            )
        )

    def test_paid_subscription_proof_blocks_before_model_probe_or_inference(self) -> None:
        response = Mock()
        response.ok = True
        response.json.return_value = {
            "success": True,
            "result": [
                {
                    "state": "Paid",
                    "price": 5,
                    "rate_plan": {"id": "pro"},
                }
            ],
        }
        with patch.object(cloudflare.requests, "get", return_value=response) as get:
            with self.assertRaisesRegex(
                cloudflare.CloudflareGoldVisionUnavailable,
                "paid/unknown",
            ):
                cloudflare._prove_workers_free("token", "a" * 32)
        get.assert_called_once()

    def test_missing_billing_read_fails_closed_before_inference(self) -> None:
        response = Mock()
        response.ok = False
        response.status_code = 403
        response.json.return_value = {
            "success": False,
            "errors": [{"code": 10000, "message": "Authentication error"}],
        }
        with patch.object(cloudflare.requests, "get", return_value=response):
            with self.assertRaisesRegex(
                cloudflare.CloudflareGoldVisionUnavailable,
                "Billing Read",
            ):
                cloudflare._prove_workers_free("token", "a" * 32)

    def test_provider_capacity_codes_never_trigger_paid_recovery(self) -> None:
        self.assertIs(
            cloudflare._cloudflare_http_code(
                429,
                {"errors": [{"code": 3036, "message": "daily free allocation exhausted"}]},
            ),
            contract.VisionErrorCode.CAPACITY,
        )
        self.assertIs(
            cloudflare._cloudflare_http_code(
                403,
                {"errors": [{"code": 5035, "message": "Workers Paid required"}]},
            ),
            contract.VisionErrorCode.CAPACITY,
        )
        self.assertIs(
            cloudflare._cloudflare_http_code(
                429,
                {"errors": [{"code": 3040, "message": "out of capacity"}]},
            ),
            contract.VisionErrorCode.PROVIDER_TRANSIENT,
        )
        self.assertIs(
            cloudflare._cloudflare_http_code(
                403,
                {"errors": [{"code": 5016, "message": "model agreement required"}]},
            ),
            contract.VisionErrorCode.AUTH_CONFIG,
        )

    def test_exact_cloudflare_model_and_one_attempt_only(self) -> None:
        ledger = BudgetLedger("film", enforce=True)
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ,
            {"CLOUDFLARE_GOLD_VISION_FREE_ONLY": "true"},
            clear=False,
        ), patch.object(
            cloudflare,
            "_credentials",
            return_value=("token", "a" * 32),
        ), patch.object(
            cloudflare,
            "_prove_workers_free",
        ) as free_proof, patch.object(
            cloudflare,
            "_prove_model_access",
        ) as model_probe, patch.object(
            cloudflare,
            "_wire_call",
            return_value={"status": "pass"},
        ) as wire:
            preview = _preview(root)
            result = cloudflare.run_gold_cloudflare_attempt(
                ledger,
                _spec(),
                preview=preview,
                narration_context="ctx",
                intended_visual="intent",
            )
            self.assertEqual(result["status"], "pass")
            free_proof.assert_called_once_with("token", "a" * 32)
            model_probe.assert_called_once_with("token", "a" * 32)
            wire.assert_called_once()
            with self.assertRaisesRegex(
                cloudflare.CloudflareGoldVisionUnavailable,
                "already attempted",
            ):
                cloudflare.run_gold_cloudflare_attempt(
                    ledger,
                    _spec(),
                    preview=preview,
                    narration_context="ctx",
                    intended_visual="intent",
                )

        summary = ledger.to_summary()["provider_attempts"]
        self.assertEqual(summary["total"], 1)
        self.assertEqual(
            summary["by_provider"],
            {cloudflare.CLOUDFLARE_VISION_PROVIDER: 1},
        )

    def test_semantic_block_is_recorded_as_content_blocked(self) -> None:
        ledger = BudgetLedger("film", enforce=True)
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ,
            {"CLOUDFLARE_GOLD_VISION_FREE_ONLY": "true"},
            clear=False,
        ), patch.object(
            cloudflare,
            "_credentials",
            return_value=("token", "a" * 32),
        ), patch.object(cloudflare, "_prove_workers_free"), patch.object(
            cloudflare,
            "_prove_model_access",
        ), patch.object(
            cloudflare,
            "_wire_call",
            return_value={"status": "block"},
        ):
            result = cloudflare.run_gold_cloudflare_attempt(
                ledger,
                _spec(),
                preview=_preview(root),
                narration_context="ctx",
                intended_visual="intent",
            )
        self.assertEqual(result["status"], "block")
        self.assertEqual(
            ledger.to_summary()["provider_attempts"]["by_outcome"],
            {"CONTENT_BLOCKED": 1},
        )

    def test_source_contains_no_billing_or_subscription_write(self) -> None:
        source = inspect.getsource(cloudflare)
        self.assertIn("/subscriptions", source)
        self.assertNotIn("requests.put(", source)
        self.assertNotIn("requests.delete(", source)
        self.assertNotIn("requests.patch(", source)
        self.assertNotIn("/ai-gateway/", source)
        self.assertNotIn("prepaid", source.casefold())
        self.assertEqual(cloudflare.CLOUDFLARE_VISION_MODEL, "@cf/qwen/qwen3.8-27b")


if __name__ == "__main__":
    unittest.main()
