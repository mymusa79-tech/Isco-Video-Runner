from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "produce-resilient-v4.yml"
TELEGRAM_WORKFLOW = ROOT / ".github" / "workflows" / "telegram-editorial-control.yml"


class SecurityV1PreproductionOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_approved_brief_gate_precedes_install_state_and_provider_secrets(self) -> None:
        gate = self.text.index("- name: Validate approved brief before secret materialization")
        install = self.text.index("- name: Install locked engine and verified local voice fallback")
        restore = self.text.index("- name: Restore encrypted cross-run memory")
        materialize = self.text.index("- name: Materialize approved production secrets")
        providers = self.text.index("- name: Verify free provider authentication")
        produce = self.text.index("- name: Produce with task-level brain and voice meshes")
        self.assertLess(gate, install)
        self.assertLess(gate, restore)
        self.assertLess(gate, materialize)
        self.assertLess(materialize, providers)
        self.assertLess(providers, produce)

    def test_brief_gate_has_no_provider_youtube_telegram_or_state_secret(self) -> None:
        gate_start = self.text.index("- name: Validate approved brief before secret materialization")
        next_step = self.text.index("\n      - name:", gate_start + 1)
        gate = self.text[gate_start:next_step]
        self.assertIn("verify_brief_approval", gate)
        self.assertIn("ISCO_APPROVED_BRIEF_SHA256", gate)
        forbidden = (
            "GEMINI_API_KEY",
            "GROQ_API_KEY",
            "OPENROUTER_API_KEY",
            "PEXELS_API_KEY",
            "PIXABAY_API_KEY",
            "YOUTUBE_API_KEY",
            "YOUTUBE_CLIENT_ID",
            "YOUTUBE_CLIENT_SECRET",
            "YOUTUBE_REFRESH_TOKEN",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "TELEGRAM_ALLOWED_USER_ID",
            "STATE_ENCRYPTION_KEY",
        )
        for name in forbidden:
            with self.subTest(name=name):
                self.assertNotIn(name, gate)

    def test_secret_materialization_requires_prevalidated_request(self) -> None:
        start = self.text.index("- name: Materialize approved production secrets")
        next_step = self.text.index("\n      - name:", start + 1)
        step = self.text[start:next_step]
        self.assertIn('test -f "$RUNNER_TEMP/isco-request.json"', step)
        self.assertNotIn("verify_brief_approval", step)
        self.assertNotIn("approved_brief.json", step)

    def test_invalid_brief_does_not_expose_telegram_notification_secrets(self) -> None:
        start = self.text.index("- name: Notify Telegram")
        next_step = self.text.index("\n      - name:", start + 1)
        step = self.text[start:next_step]
        self.assertIn("if: always() && steps.validate_brief.outcome == 'success'", step)
        self.assertIn("TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}", step)

    def test_plaintext_production_secrets_and_state_are_cleaned_last(self) -> None:
        notify = self.text.index("- name: Notify Telegram")
        cleanup = self.text.index("- name: Remove plaintext production secrets and state")
        self.assertLess(notify, cleanup)
        block = self.text[cleanup:]
        self.assertIn('rm -rf "$RUNNER_TEMP/isco-secrets"', block)
        self.assertIn('rm -rf "$RUNNER_TEMP/isco-state"', block)
        self.assertIn('rm -f "$RUNNER_TEMP/isco-request.json"', block)
        self.assertIn('rm -f "$RUNNER_TEMP/piper-preflight.wav"', block)
        self.assertNotIn("\n      - name:", block[len("- name: Remove plaintext production secrets and state"):])

    def test_workflow_dispatch_has_no_topic_or_format_inputs(self) -> None:
        header = self.text[: self.text.index("jobs:")]
        self.assertRegex(header, r"on:\s*\n\s+workflow_dispatch:\s*\n")
        self.assertNotIn("inputs:", header)
        self.assertNotRegex(self.text, r"inputs\.(?:topic|format)")

    def test_private_engine_checkout_does_not_persist_credentials(self) -> None:
        marker = "repository: mymusa79-tech/Isco-Video-Agent"
        start = self.text.index(marker)
        block = self.text[start : self.text.index("\n\n      - name:", start)]
        self.assertIn("persist-credentials: false", block)
        self.assertRegex(block, r"ref:\s+[0-9a-f]{40}")


class TelegramControlSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = TELEGRAM_WORKFLOW.read_text(encoding="utf-8")

    def test_control_plane_production_requires_explicit_durable_authorization(self) -> None:
        self.assertIn('CONTROL_PLANE_PRODUCTION_ENABLED: "true"', self.text)
        self.assertIn("actions: write", self.text)
        reserve = self.text.index("- name: Reserve explicit production dispatch")
        persist = self.text.index("- name: Persist dispatch reservation before workflow dispatch")
        dispatch = self.text.index("- name: Dispatch one explicitly reserved production request")
        self.assertLess(reserve, persist)
        self.assertLess(persist, dispatch)
        self.assertIn("gh workflow run telegram-production-request.yml", self.text)
        self.assertIn('-f authorization_id="$AUTHORIZATION_ID"', self.text)
        self.assertNotIn("actions/workflows/produce-resilient-v4.yml/dispatches", self.text)
        self.assertNotIn("python scripts/run_control_production.py", self.text)

    def test_state_encryption_requires_dedicated_key_without_bot_token_fallback(self) -> None:
        dedicated = "STATE_ENCRYPTION_KEY: ${{ secrets.STATE_ENCRYPTION_KEY }}"
        self.assertNotIn("secrets.STATE_ENCRYPTION_KEY || secrets.TELEGRAM_BOT_TOKEN", self.text)
        dedicated_count = self.text.count(dedicated)
        self.assertGreaterEqual(dedicated_count, 3)
        self.assertEqual(self.text.count("-pass env:STATE_ENCRYPTION_KEY"), dedicated_count)

    def test_research_runtime_verifies_supply_chain_before_pip_install(self) -> None:
        preflight = self.text.index("security_v1_supply_chain_preflight.py lock")
        pip_install = self.text.index("python -m pip install -r engine/requirements-lock.txt")
        self.assertLess(preflight, pip_install)

    def test_private_engine_research_checkout_does_not_persist_credentials(self) -> None:
        marker = "repository: mymusa79-tech/Isco-Video-Agent"
        start = self.text.index(marker)
        block = self.text[start : self.text.index("\n\n      - name:", start)]
        self.assertIn("persist-credentials: false", block)


if __name__ == "__main__":
    unittest.main()

# CI retrigger after live-tree reconciliation; no production behavior change.
