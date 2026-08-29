from __future__ import annotations

import unittest
from pathlib import Path

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.ai_budget import BudgetLedger
import scripts.tts_cache_budget_accounting as accounting


class TtsCacheBudgetAccountingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_boundary = orchestrator._synthesize_tts_section
        self.original_accounting_boundary = accounting._original_tts_boundary

    def tearDown(self) -> None:
        orchestrator._synthesize_tts_section = self.original_boundary
        accounting._original_tts_boundary = self.original_accounting_boundary

    def test_cache_style_return_is_one_logical_tts_task_and_zero_provider_attempts(self) -> None:
        output = Path("cached.wav")

        def cache_hit(
            ledger,
            circuit,
            budget,
            *,
            task_id: str,
            api_key: str,
            transcript: str,
            output: Path,
            model: str,
            voice: str,
            style: str,
        ) -> Path:
            return output

        cache_hit._isco_tts_durable_section_cache = True
        orchestrator._synthesize_tts_section = cache_hit
        accounting.install_tts_cache_budget_accounting()
        ledger = BudgetLedger("film", enforce=True)

        result = orchestrator._synthesize_tts_section(
            ledger,
            object(),
            object(),
            task_id="TTS_SECTION_03",
            api_key="not-used-on-hit",
            transcript="نص مستعاد",
            output=output,
            model="gemini-3.1-flash-tts-preview",
            voice="Charon",
            style="Emotion: calm.",
        )

        self.assertEqual(result, output)
        summary = ledger.to_summary()
        self.assertEqual(summary["logical_tasks"]["total"], 1)
        self.assertEqual(summary["logical_tasks"]["by_kind"]["TTS_SECTION"], 1)
        self.assertEqual(summary["by_capability"]["tts"]["logical_tasks"], 1)
        self.assertEqual(summary["provider_attempts"]["total"], 0)
        self.assertEqual(summary["by_capability"]["tts"]["provider_attempts"], 0)

    def test_non_tts_boundary_is_not_registered(self) -> None:
        def passthrough(*_args, **kwargs):
            return kwargs["output"]

        orchestrator._synthesize_tts_section = passthrough
        accounting.install_tts_cache_budget_accounting()
        ledger = BudgetLedger("film", enforce=True)
        orchestrator._synthesize_tts_section(
            ledger,
            object(),
            object(),
            task_id="OTHER_TASK",
            api_key="unused",
            transcript="x",
            output=Path("other.wav"),
            model="m",
            voice="v",
            style="s",
        )
        self.assertEqual(ledger.to_summary()["logical_tasks"]["total"], 0)

    def test_install_is_idempotent(self) -> None:
        def boundary(*_args, **kwargs):
            return kwargs["output"]

        orchestrator._synthesize_tts_section = boundary
        accounting.install_tts_cache_budget_accounting()
        first = orchestrator._synthesize_tts_section
        accounting.install_tts_cache_budget_accounting()
        self.assertIs(orchestrator._synthesize_tts_section, first)


if __name__ == "__main__":
    unittest.main()
