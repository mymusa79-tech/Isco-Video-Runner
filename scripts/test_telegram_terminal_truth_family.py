from __future__ import annotations

import unittest
from pathlib import Path

from scripts import telegram_final_notify as final_notify


ROOT = Path(__file__).resolve().parents[1]
LIVE_WORKER = ROOT / "cloudflare" / "telegram-control-worker" / "observability-worker-v6.js"


class TelegramTerminalTruthFamilyTests(unittest.TestCase):
    """Run197: terminal GitHub state must outrank stale progress/display hints."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.worker = LIVE_WORKER.read_text(encoding="utf-8")

    def test_terminal_github_state_is_resolved_before_runtime_progress(self) -> None:
        start = self.worker.index("function productionView(value)")
        end = self.worker.index("function detailedStageState(value)", start)
        production_view = self.worker[start:end]
        terminal = production_view.index('if (String(run.status || "") === "completed")')
        progress = production_view.index("if (progress) {")
        self.assertLess(terminal, progress)
        self.assertIn('return { headline: `${conclusion === "success" ? "✅" : "❌"} ${label}`', production_view)

    def test_stale_lifecycle_stage_hint_cannot_override_completed_run(self) -> None:
        start = self.worker.index("async function showLiveStatus(env, target, stageHint = \"\")")
        end = self.worker.index("async function showStageDetails", start)
        live_status = self.worker[start:end]
        self.assertIn('String(value.run.status || "") !== "completed"', live_status)
        self.assertIn("!value.progress && INTERNAL_STAGE_ORDER.includes(stageHint)", live_status)

    def test_runtime_progress_is_exact_run_bound(self) -> None:
        self.assertIn('String(value.run_id || "") !== String(run.id)', self.worker)
        self.assertIn('cache: "no-store"', self.worker)

    def test_successful_terminal_edit_does_not_emit_duplicate_fallback_message(self) -> None:
        calls: list[str] = []

        def fake_request(token: str, method: str, payload: dict[str, object]) -> bool:
            calls.append(method)
            return True

        original = final_notify._telegram_request
        final_notify._telegram_request = fake_request
        try:
            ok = final_notify.deliver_terminal_message(
                token="tok",
                chat_id="chat",
                text="terminal",
                progress_message_id="42",
            )
        finally:
            final_notify._telegram_request = original

        self.assertTrue(ok)
        self.assertEqual(calls, ["editMessageText"])

    def test_failed_terminal_edit_has_one_send_fallback(self) -> None:
        calls: list[str] = []

        def fake_request(token: str, method: str, payload: dict[str, object]) -> bool:
            calls.append(method)
            return method == "sendMessage"

        original = final_notify._telegram_request
        final_notify._telegram_request = fake_request
        try:
            ok = final_notify.deliver_terminal_message(
                token="tok",
                chat_id="chat",
                text="terminal",
                progress_message_id="42",
            )
        finally:
            final_notify._telegram_request = original

        self.assertTrue(ok)
        self.assertEqual(calls, ["editMessageText", "sendMessage"])


if __name__ == "__main__":
    unittest.main()
