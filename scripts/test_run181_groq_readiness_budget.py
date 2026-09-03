from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from isco_video_agent.ai_budget import BudgetLedger, Capability, Priority, TaskSpec
from scripts import provider_health_registry as health
from scripts import run181_vision_mesh_closure as run181
from scripts import vision_provider_reliability as legacy
from scripts import vision_stage_contract_v2 as contract
from scripts import vision_stage_transport_v2 as transport


_PASS = {
    "status": "pass",
    "relevance": 0.99,
    "quality": 0.99,
    "risk": 0.01,
    "reasons": [],
}


def _spec(max_attempts: int = 1) -> TaskSpec:
    return TaskSpec(
        task_id="VISUAL_AUDIT_RUN181_GROQ_READINESS",
        kind="VISUAL_AUDIT",
        priority=Priority.P0,
        capability=Capability.VISION,
        max_provider_attempts=max_attempts,
        schema_repair_allowed=False,
        local_fallback=False,
        semantic_block_is_final=True,
    )


def _preview(temp_dir: str) -> Path:
    path = Path(temp_dir) / "preview.mp4"
    path.write_bytes(b"run181-readiness-placeholder")
    return path


class Run181GroqReadinessBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        health.reset_provider_health()
        run181._GROQ_MODEL_CERTIFIED.set(None)

    def tearDown(self) -> None:
        health.reset_provider_health()
        run181._GROQ_MODEL_CERTIFIED.set(None)

    def test_missing_groq_key_is_seeded_as_zero_inference_readiness(self) -> None:
        with mock.patch.object(run181, "_groq_key", return_value=""):
            transport._seed_static_groq_readiness()
        evidence = health.provider_unavailable(
            "groq",
            model=run181.GROQ_VISION_MODEL,
            quota_domain=run181.GROQ_VISION_QUOTA_DOMAIN,
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.source, "vision_static_readiness")
        self.assertEqual(evidence.reason, "Groq key unavailable for Vision fallback")

    def test_missing_groq_key_preserves_three_real_attempt_v2_budget(self) -> None:
        ledger = BudgetLedger("moment", enforce=True)
        first_schema = contract.VisionStageError(
            contract.VisionErrorCode.STRUCTURAL_INVALID,
            "missing field",
            provider="openrouter",
            requested_model=contract.OPENROUTER_PRIMARY_MODEL,
            resolved_model="model/first:free",
        )
        with tempfile.TemporaryDirectory() as temp_dir, legacy.vision_provider_circuit_scope(), mock.patch.object(
            run181, "_groq_key", return_value=""
        ), mock.patch.object(
            run181, "refresh_runtime_provider_health", return_value=None
        ), mock.patch.object(
            contract,
            "_openrouter_call",
            side_effect=[first_schema, (dict(_PASS), "model/alternate:free")],
        ) as openrouter, mock.patch.object(
            contract,
            "_discover_alternate_free_vision_model",
            return_value="model/alternate:free",
        ):
            transport._seed_static_groq_readiness()
            result = run181._route_visual_audit_v3(
                ledger,
                _spec(max_attempts=3),
                "gemini",
                "gemini-3.7-flash",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("Gemini timeout")),
                "gem-key",
                _preview(temp_dir),
                narration_context="ctx",
                intended_visual="intent",
            )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(openrouter.call_count, 2)
        summary = ledger.to_summary()["provider_attempts"]
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["by_provider"], {"gemini": 1, "openrouter": 2})


if __name__ == "__main__":
    unittest.main()
