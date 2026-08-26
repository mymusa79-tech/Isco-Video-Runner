from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts import telegram_final_notify as final_notify
from scripts import telegram_status_model as status_model
from scripts import telegram_status_projection as projection

ROOT = Path(__file__).resolve().parents[1]
WORKER_DIR = ROOT / "cloudflare" / "telegram-control-worker"
WRANGLER = WORKER_DIR / "wrangler.toml.example"
OBSERVABILITY_WORKER = WORKER_DIR / "observability-worker.js"
GENERATED_CONTRACT = WORKER_DIR / "status-contract.generated.js"
CANONICAL_CONTRACT = ROOT / "scripts" / "telegram_status_contract.json"
GENERATOR = ROOT / "scripts" / "generate_telegram_status_contract_js.py"
CONTROL_WORKFLOW = ROOT / ".github" / "workflows" / "telegram-editorial-control.yml"


class CanonicalStatusContractTests(unittest.TestCase):
    def test_worker_contract_is_generated_from_canonical_json(self) -> None:
        namespace: dict[str, object] = {"__file__": str(GENERATOR), "__name__": "telegram_status_contract_test"}
        exec(GENERATOR.read_text(encoding="utf-8"), namespace)
        rendered = namespace["render"]()
        self.assertEqual(rendered, GENERATED_CONTRACT.read_text(encoding="utf-8"))

    def test_python_status_model_uses_canonical_stage_rules(self) -> None:
        self.assertEqual(status_model.stage_for_step("Install locked Engine runtime")["label"], "تهيئة الإنتاج")
        self.assertEqual(
            status_model.stage_for_step("Run exact approved Telegram production")["label"],
            "الإنتاج: التخطيط → الكتابة → الصوت → المونتاج",
        )
        self.assertEqual(status_model.stage_for_step("Final Review quality gate")["label"], "فحص الجودة")
        self.assertEqual(status_model.terminal_state("failure"), {"label": "فشل", "icon": "❌"})

    def test_no_retry_action_exists_in_canonical_contract(self) -> None:
        text = CANONICAL_CONTRACT.read_text(encoding="utf-8")
        self.assertNotIn('"retry"', text)


class SanitizedProjectionTests(unittest.TestCase):
    def test_projection_contains_counts_and_hashes_but_no_sensitive_content(self) -> None:
        state = {
            "last_event_at": "2026-08-26T20:00:00Z",
            "active_research_session_id": "secret-session-id",
            "production_target": {"request_id": "request-secret", "request_sha256": "secret-sha"},
            "saved_suggestions": [
                {"status": "available", "candidate": {"title": "سري جدًا"}},
                {"status": "expired", "candidate": {"title": "قديم"}},
            ],
            "used_topics": [{"topic": "موضوع حساس", "kind": "long"}],
            "requests": {"request-secret": {"approved_topic": "عنوان خاص"}},
            "pending_actions": [{"payload": "do-not-export"}],
            "production_queue": [{"request_id": "request-secret"}],
        }
        value = projection.build_projection(state)
        encoded = json.dumps(value, ensure_ascii=False)
        self.assertEqual(value["editorial"]["saved_count"], 1)
        self.assertEqual(value["editorial"]["used_count"], 1)
        self.assertTrue(value["editorial"]["approved_target"])
        self.assertEqual(len(value["editorial"]["approved_request_hash"]), 12)
        for secret in (
            "secret-session-id",
            "request-secret",
            "secret-sha",
            "سري جدًا",
            "موضوع حساس",
            "عنوان خاص",
            "do-not-export",
        ):
            self.assertNotIn(secret, encoded)

    def test_projection_is_byte_stable_when_state_has_no_event_time(self) -> None:
        state = {"saved_suggestions": [], "used_topics": []}
        first = projection.build_projection(state)
        second = projection.build_projection(state)
        self.assertEqual(first, second)
        self.assertEqual(first["generated_at"], "1970-01-01T00:00:00Z")

    def test_explicit_generated_time_is_normalized_to_utc(self) -> None:
        value = projection.build_projection({}, generated_at=datetime(2026, 8, 26, 20, 37, tzinfo=timezone.utc))
        self.assertEqual(value["generated_at"], "2026-08-26T20:37:00Z")


class EdgeObservabilityContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.worker = OBSERVABILITY_WORKER.read_text(encoding="utf-8")
        cls.wrangler = WRANGLER.read_text(encoding="utf-8")

    def test_observability_wrapper_is_active_entrypoint(self) -> None:
        self.assertIn('main = "observability-worker.js"', self.wrangler)
        self.assertIn('import baseWorker from "./index.js"', self.worker)
        self.assertIn('import { STATUS_CONTRACT } from "./status-contract.generated.js"', self.worker)

    def test_wrapper_has_valid_javascript_syntax_when_node_is_available(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")
        subprocess.run([node, "--check", str(OBSERVABILITY_WORKER)], check=True, capture_output=True, text=True)
        subprocess.run([node, "--check", str(GENERATED_CONTRACT)], check=True, capture_output=True, text=True)

    def test_global_refresh_is_read_only_and_never_dispatches(self) -> None:
        self.assertIn('callback_data: "cmd:refresh_all"', self.worker)
        self.assertIn('data === "cmd:refresh_all"', self.worker)
        self.assertIn("Promise.all", self.worker)
        self.assertIn("SOURCE_CACHE", self.worker)
        self.assertIn("آخر حالة معروفة", self.worker)
        self.assertNotIn("dispatchToGitHub", self.worker)
        self.assertNotIn("workflow_dispatch", self.worker)
        self.assertNotIn("telegram-production-request.yml", self.worker)
        self.assertNotIn("produce-resilient-v4.yml", self.worker)
        self.assertNotIn("cmd:retry", self.worker)

    def test_wrapper_preserves_webhook_secret_and_identity_boundaries(self) -> None:
        for marker in (
            "X-Telegram-Bot-Api-Secret-Token",
            "TELEGRAM_WEBHOOK_SECRET",
            "TELEGRAM_ALLOWED_USER_ID",
            "TELEGRAM_CHAT_ID",
            "secretHeaderValid(request, env)",
            "authorized(update, env)",
        ):
            self.assertIn(marker, self.worker)

    def test_dashboard_has_fresh_stale_unavailable_and_health_sources(self) -> None:
        for marker in (
            'state: "fresh"',
            'state: "stale"',
            'state: "unavailable"',
            "getWebhookInfo",
            "pending_update_count",
            "telegram-status.json?ref=control-plane-state",
            "telegram-editorial-control.yml/runs?per_page=1",
            "آخر تحقق شامل",
        ):
            self.assertIn(marker, self.worker)

    def test_stats_leaf_has_local_and_global_refresh(self) -> None:
        self.assertIn('{ text: "🔄 تحديث", callback_data: currentRefresh }', self.worker)
        self.assertIn('{ text: "🔄 تحديث الكل", callback_data: "cmd:refresh_all" }', self.worker)


class EditorialProjectionWorkflowTests(unittest.TestCase):
    def test_control_workflow_persists_sanitized_projection(self) -> None:
        text = CONTROL_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("Build sanitized Telegram read projection", text)
        self.assertIn("Persist sanitized Telegram read projection", text)
        self.assertIn("scripts/telegram_status_projection.py", text)
        self.assertIn("state/telegram-status.json", text)
        self.assertIn("Sanitized Telegram read projection unchanged", text)


class TerminalDeliveryObservabilityTests(unittest.TestCase):
    def test_failed_terminal_edit_uses_exactly_one_send_fallback(self) -> None:
        calls: list[str] = []

        def fake_request(token: str, method: str, payload: dict[str, str]) -> bool:
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

    def test_failed_edit_and_failed_fallback_is_observable_failure(self) -> None:
        calls: list[str] = []

        def fake_request(token: str, method: str, payload: dict[str, str]) -> bool:
            calls.append(method)
            return False

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
        self.assertFalse(ok)
        self.assertEqual(calls, ["editMessageText", "sendMessage"])


if __name__ == "__main__":
    unittest.main()
