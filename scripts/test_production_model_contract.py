from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

import isco_video_agent.orchestrator as orchestrator
from scripts.production_model_contract import (
    CANONICAL_CONTENT_MODEL,
    CANONICAL_TTS_MODEL,
    install_production_model_contract,
)


WORKFLOW = Path(".github/workflows/produce-resilient-v4.yml")


class ProductionModelContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._content_models = set(orchestrator.FREE_CONTENT_MODELS)
        self._tts_models = set(orchestrator.FREE_TTS_MODELS)

    def tearDown(self) -> None:
        orchestrator.FREE_CONTENT_MODELS = set(self._content_models)
        orchestrator.FREE_TTS_MODELS = set(self._tts_models)

    def test_run135_exact_model_is_accepted_by_real_engine_guard(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GEMINI_CONTENT_MODEL": CANONICAL_CONTENT_MODEL,
                "GEMINI_TTS_MODEL": CANONICAL_TTS_MODEL,
            },
            clear=False,
        ):
            result = install_production_model_contract(orchestrator)

        self.assertEqual(result["content_model"], CANONICAL_CONTENT_MODEL)
        self.assertEqual(result["network_content_model"], CANONICAL_CONTENT_MODEL)
        self.assertEqual(result["tts_model"], CANONICAL_TTS_MODEL)
        orchestrator._enforce_free_only_models(
            CANONICAL_CONTENT_MODEL,
            CANONICAL_TTS_MODEL,
        )

    def test_production_policy_replaces_legacy_raw_whitelist(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GEMINI_CONTENT_MODEL": CANONICAL_CONTENT_MODEL,
                "GEMINI_TTS_MODEL": CANONICAL_TTS_MODEL,
            },
            clear=False,
        ):
            install_production_model_contract(orchestrator)

        self.assertEqual(orchestrator.FREE_CONTENT_MODELS, {CANONICAL_CONTENT_MODEL})
        self.assertEqual(orchestrator.FREE_TTS_MODELS, {CANONICAL_TTS_MODEL})
        with self.assertRaisesRegex(RuntimeError, "Free-only policy blocked content model"):
            orchestrator._enforce_free_only_models("gemini-2.5-flash", CANONICAL_TTS_MODEL)

    def test_workflow_and_runtime_contract_are_the_same_explicit_model(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(
            text.count(f"GEMINI_CONTENT_MODEL: {CANONICAL_CONTENT_MODEL}"),
            2,
        )
        self.assertEqual(
            text.count(f"GEMINI_TTS_MODEL: {CANONICAL_TTS_MODEL}"),
            2,
        )

    def test_noncanonical_content_model_fails_before_provider_work(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GEMINI_CONTENT_MODEL": "gemini-2.5-flash",
                "GEMINI_TTS_MODEL": CANONICAL_TTS_MODEL,
            },
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "content model contract drift"):
                install_production_model_contract(orchestrator)


if __name__ == "__main__":
    unittest.main()
