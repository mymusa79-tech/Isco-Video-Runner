from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EDITORIAL = ROOT / ".github" / "workflows" / "telegram-editorial-control.yml"
OUTBOX_SEND = ROOT / ".github" / "workflows" / "telegram-outbox-send.yml"
OUTBOX_RECONCILE = ROOT / ".github" / "workflows" / "telegram-outbox-reconcile.yml"
PRODUCTION = ROOT / ".github" / "workflows" / "produce-resilient-v4.yml"
PUBLISH_GATE = ROOT / "scripts" / "telegram_publish_gate.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _step(text: str, name: str, next_name: str) -> str:
    start = text.index(f"- name: {name}")
    end = text.index(f"- name: {next_name}", start)
    return text[start:end]


class TelegramL6WorkflowContractTests(unittest.TestCase):
    def test_webhook_active_suppresses_fallback_poll_for_every_trigger_mode(self) -> None:
        text = _text(EDITORIAL)
        poll = _step(
            text,
            "Poll or replay authorized Telegram commands",
            "Checkout exact private Engine for research only",
        )
        self.assertIn("if python scripts/telegram_webhook_replay.py webhook-active; then", poll)
        self.assertIn("getUpdates polling is suppressed for every trigger mode", poll)
        self.assertNotIn('GITHUB_EVENT_NAME" = "schedule', poll)
        self.assertIn("release-approval-only", poll)

    def test_active_production_allows_only_release_approval_and_persists_it(self) -> None:
        text = _text(EDITORIAL)
        poll = _step(
            text,
            "Poll or replay authorized Telegram commands",
            "Checkout exact private Engine for research only",
        )
        self.assertIn("release_approval_consumed=true", poll)
        self.assertIn("non-release stateful Telegram command remains read-only", poll)
        persist = _step(text, "Persist encrypted control-panel state", "Build sanitized Telegram read projection")
        self.assertIn("steps.poll.outputs.release_approval_consumed == 'true'", persist)
        projection = _step(text, "Build sanitized Telegram read projection", "Persist sanitized Telegram read projection")
        self.assertIn("steps.poll.outputs.release_approval_consumed == 'true'", projection)
        self.assertIn("steps.persist_control_state.outcome == 'success'", projection)

    def test_publish_gate_never_owns_live_telegram_ingress_or_bot_secret(self) -> None:
        gate = _text(PUBLISH_GATE)
        for forbidden in ("getUpdates", "TELEGRAM_BOT_TOKEN", "api.telegram.org"):
            self.assertNotIn(forbidden, gate)
        production = _text(PRODUCTION)
        step = _step(production, "Request publish approval via Telegram", "Create GitHub Release with unified delivery bundle")
        self.assertIn("GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}", step)
        self.assertIn("TELEGRAM_OUTBOX_REF: ${{ github.ref_name }}", step)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", step)
        self.assertNotIn("TELEGRAM_CHAT_ID", step)
        self.assertNotIn("TELEGRAM_ALLOWED_USER_ID", step)

    def test_outbox_send_is_the_only_l6_provider_sender_and_is_serialized(self) -> None:
        send = _text(OUTBOX_SEND)
        reconcile = _text(OUTBOX_RECONCILE)
        self.assertIn("group: telegram-editorial-control", send)
        self.assertIn("group: telegram-editorial-control", reconcile)
        self.assertIn("TELEGRAM_BOT_TOKEN", send)
        for forbidden in ("TELEGRAM_BOT_TOKEN", "api.telegram.org", "sendMessage"):
            self.assertNotIn(forbidden, reconcile)

    def test_release_candidate_and_release_transaction_share_capability_evidence(self) -> None:
        production = _text(PRODUCTION)
        self.assertGreaterEqual(production.count("capability-manifest.json"), 4)
        self.assertIn('"$FINAL_OUTPUT_ROOT/capability-manifest.json"', production)
        self.assertIn("actions: write", production)


if __name__ == "__main__":
    unittest.main()
