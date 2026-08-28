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
STATE_SECRET_WORKFLOW = ROOT / ".github" / "workflows" / "telegram-edge-state-secret.yml"
TOPIC_MEMORY_UI = ROOT / "scripts" / "telegram_topic_memory_ui.py"


class CanonicalStatusContractTests(unittest.TestCase):
    def test_worker_contract_is_generated_from_canonical_json(self) -> None:
        namespace: dict[str, object] = {"__file__": str(GENERATOR), "__name__": "telegram_status_contract_test"}
        exec(GENERATOR.read_text(encoding="utf-8"), namespace)
        self.assertEqual(namespace["render"](), GENERATED_CONTRACT.read_text(encoding="utf-8"))

    def test_python_status_model_uses_canonical_stage_rules(self) -> None:
        self.assertEqual(status_model.stage_for_step("Install locked Engine runtime")["label"], "تهيئة الإنتاج")
        self.assertEqual(status_model.stage_for_step("Run exact approved Telegram production")["label"], "الإنتاج: التخطيط → الكتابة → الصوت → المونتاج")
        self.assertEqual(status_model.terminal_state("failure"), {"label": "فشل", "icon": "❌"})

    def test_no_retry_action_exists_in_canonical_contract(self) -> None:
        self.assertNotIn('"retry"', CANONICAL_CONTRACT.read_text(encoding="utf-8"))


class SanitizedProjectionTests(unittest.TestCase):
    def test_projection_contains_safe_counts_only(self) -> None:
        state = {
            "last_event_at": "2026-08-26T20:00:00Z",
            "active_research_session_id": "secret-session-id",
            "production_target": {"request_id": "request-secret", "request_sha256": "secret-sha"},
            "saved_suggestions": [
                {"status": "available", "kind": "long", "candidate": {"title": "سري جدًا"}},
                {"status": "available", "kind": "short", "candidate": {"title": "شورت سري"}},
                {"status": "expired", "kind": "long", "candidate": {"title": "قديم"}},
            ],
            "used_topics": [
                {"topic": "موضوع حساس", "kind": "long"},
                {"topic": "موضوع حساس 2", "kind": "short"},
            ],
            "requests": {"request-secret": {"approved_topic": "عنوان خاص"}},
            "pending_actions": [{"payload": "do-not-export"}],
            "production_queue": [{"request_id": "request-secret"}],
        }
        value = projection.build_projection(state)
        editorial = value["editorial"]
        self.assertEqual(editorial["saved_count"], 2)
        self.assertEqual(editorial["saved_long_count"], 1)
        self.assertEqual(editorial["saved_short_count"], 1)
        self.assertEqual(editorial["used_count"], 2)
        self.assertEqual(editorial["used_long_count"], 1)
        self.assertEqual(editorial["used_short_count"], 1)
        encoded = json.dumps(value, ensure_ascii=False)
        for secret in ("secret-session-id", "request-secret", "secret-sha", "سري جدًا", "شورت سري", "موضوع حساس", "عنوان خاص", "do-not-export"):
            self.assertNotIn(secret, encoded)

    def test_projection_is_byte_stable_without_event_time(self) -> None:
        first = projection.build_projection({"saved_suggestions": [], "used_topics": []})
        second = projection.build_projection({"saved_suggestions": [], "used_topics": []})
        self.assertEqual(first, second)
        self.assertEqual(first["generated_at"], "1970-01-01T00:00:00Z")

    def test_explicit_generated_time_is_normalized(self) -> None:
        value = projection.build_projection({}, generated_at=datetime(2026, 8, 26, 20, 37, tzinfo=timezone.utc))
        self.assertEqual(value["generated_at"], "2026-08-26T20:37:00Z")


class EdgeObservabilityContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.worker = OBSERVABILITY_WORKER.read_text(encoding="utf-8")
        cls.wrangler = WRANGLER.read_text(encoding="utf-8")
        cls.topic_ui = TOPIC_MEMORY_UI.read_text(encoding="utf-8")

    def test_observability_wrapper_is_active_entrypoint(self) -> None:
        self.assertIn('main = "observability-worker.js"', self.wrangler)
        self.assertIn('import baseWorker from "./index.js"', self.worker)
        self.assertIn('import { STATUS_CONTRACT } from "./status-contract.generated.js"', self.worker)
        self.assertIn('observability: "v2"', self.worker)

    def test_worker_has_valid_javascript_syntax_when_node_is_available(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")
        subprocess.run([node, "--check", str(OBSERVABILITY_WORKER)], check=True, capture_output=True, text=True)
        subprocess.run([node, "--check", str(GENERATED_CONTRACT)], check=True, capture_output=True, text=True)

    def test_python_only_array_method_regression_is_closed(self) -> None:
        self.assertNotIn("lines.extend(", self.worker)
        self.assertIn("lines.push(", self.worker)

    def test_navigation_and_library_reads_are_edge_fast(self) -> None:
        for marker in (
            'data === "cmd:menu"',
            'data === "cmd:search_menu"',
            'data === "cmd:library_menu"',
            'data === "cmd:stats_menu"',
            'data === "cmd:status"',
            'data === "cmd:refresh_all"',
            'data === "cmd:saved"',
            'data === "cmd:used"',
            "pageSpec(data)",
            "controlState(env)",
            "editMessageText",
            "message is not modified",
        ):
            self.assertIn(marker, self.worker)
        self.assertNotIn("dispatchToGitHub", self.worker)
        self.assertNotIn("workflow_dispatch", self.worker)

    def test_encrypted_library_state_is_read_only_and_private(self) -> None:
        for marker in (
            "STATE_ENCRYPTION_KEY",
            "control-plane-state/state/control-panel.json.enc",
            '"PBKDF2"',
            '"AES-CBC"',
            "iterations: 10000",
            "STATE_TTL_MS",
        ):
            self.assertIn(marker, self.worker)
        self.assertNotIn("control-panel.json\"", self.worker)

    def test_security_boundaries_are_preserved(self) -> None:
        for marker in (
            "X-Telegram-Bot-Api-Secret-Token",
            "TELEGRAM_WEBHOOK_SECRET",
            "TELEGRAM_ALLOWED_USER_ID",
            "TELEGRAM_CHAT_ID",
            "secretHeaderValid(request, env)",
            "authorized(update, env)",
        ):
            self.assertIn(marker, self.worker)

    def test_global_refresh_is_read_only(self) -> None:
        self.assertIn("Promise.all", self.worker)
        self.assertIn('telegram(env, "getWebhookInfo"', self.worker)
        self.assertIn("pending_update_count", self.worker)
        self.assertIn("control-plane-state/state/telegram-status.json", self.worker)
        self.assertIn("آخر تحقق شامل", self.worker)
        self.assertNotIn("telegram-production-request.yml", self.worker)
        self.assertNotIn("cmd:retry", self.worker)

    def test_library_callback_contract_matches_python_ui(self) -> None:
        for callback in (
            "cmd:saved-long",
            "cmd:saved-short",
            "cmd:used-long",
            "cmd:used-short",
            "cmd:saved-long-page-",
            "cmd:saved-short-page-",
            "cmd:used-long-page-",
            "cmd:used-short-page-",
            "cmd:savedpick-",
        ):
            self.assertIn(callback, self.topic_ui)
            if callback != "cmd:savedpick-":
                self.assertIn(callback.split("-page-")[0], self.worker)
        self.assertIn("cmd:savedpick-", self.worker)


class EdgeSecretInstallationTests(unittest.TestCase):
    def test_state_key_is_installed_without_paid_storage(self) -> None:
        text = STATE_SECRET_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("STATE_ENCRYPTION_KEY", text)
        self.assertIn("wrangler@${WRANGLER_VERSION}", text)
        self.assertIn("secret put STATE_ENCRYPTION_KEY", text)
        self.assertIn("group: telegram-edge-deploy", text)
        self.assertNotIn("kv_namespaces", text)
        self.assertNotIn("d1_databases", text)
        self.assertNotIn("r2_buckets", text)


class EditorialProjectionWorkflowTests(unittest.TestCase):
    def test_control_workflow_persists_sanitized_projection(self) -> None:
        text = CONTROL_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("Build sanitized Telegram read projection", text)
        self.assertIn("Persist sanitized Telegram read projection", text)
        self.assertIn("scripts/telegram_status_projection.py", text)
        self.assertIn("state/telegram-status.json", text)


class TerminalDeliveryObservabilityTests(unittest.TestCase):
    def test_failed_terminal_edit_uses_one_send_fallback(self) -> None:
        calls: list[str] = []

        def fake_request(token: str, method: str, payload: dict[str, str]) -> bool:
            calls.append(method)
            return method == "sendMessage"

        original = final_notify._telegram_request
        final_notify._telegram_request = fake_request
        try:
            ok = final_notify.deliver_terminal_message(token="tok", chat_id="chat", text="terminal", progress_message_id="42")
        finally:
            final_notify._telegram_request = original
        self.assertTrue(ok)
        self.assertEqual(calls, ["editMessageText", "sendMessage"])


if __name__ == "__main__":
    unittest.main()
