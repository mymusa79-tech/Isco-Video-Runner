from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import telegram_clean_v2_control as control


def _request() -> dict:
    request = {
        "schema_version": 1,
        "request_id": "req-history",
        "source": "clean_v2_telegram_editorial_lite",
        "scope": "short",
        "approved_by_user": True,
        "approved_topic": "موضوع محفوظ للاستئناف",
        "research_pack": [{"source_title": "مصدر", "source_url": "https://example.test"}],
        "idea_id": "idea-1",
        "selected_at": "2026-10-01T00:00:00Z",
        "status": "dispatched",
        "confirmed_at": "2026-10-01T00:01:00Z",
        "dispatched_at": "2026-10-01T00:02:00Z",
    }
    request["request_sha256"] = control._request_hash(request)
    request["production"] = {
        "run_id": "42",
        "run_attempt": 1,
        "runner_sha": "runner",
        "engine_sha": "engine",
        "resume_cache_key": "cache-42",
        "last_job_status": "failure",
        "manifest_status": "failed",
        "final_published": False,
    }
    return request


class TelegramResumeHistoryIntegrationTests(unittest.TestCase):
    def test_history_detail_shows_resume_only_when_verified(self):
        request = _request()
        state = control.default_state()
        state["requests"][request["request_id"]] = request
        decision = {
            "available": True,
            "reason": "",
            "completed_stage": "text_audit",
            "stage_label": "Text Audit",
            "run_id": "42",
        }
        with mock.patch.object(control, "_resume_decision_for_request", return_value=decision):
            text, keyboard = control._history_request_view(state, request["request_id"])
        callbacks = [button["callback_data"] for row in keyboard for button in row]
        self.assertIn("سيستكمل من: Text Audit", text)
        self.assertIn("resume:req-history", callbacks)
        self.assertIn("restart:req-history", callbacks)

    def test_disabled_resume_has_specific_reason_and_only_restart_action(self):
        request = _request()
        state = control.default_state()
        state["requests"][request["request_id"]] = request
        decision = {
            "available": False,
            "reason": "checkpoint غير موجود في Actions cache؛ انتهت صلاحيته أو لم يُحفظ.",
            "completed_stage": "",
            "stage_label": "",
            "run_id": "42",
        }
        with mock.patch.object(control, "_resume_decision_for_request", return_value=decision):
            text, keyboard = control._history_request_view(state, request["request_id"])
        callbacks = [
            button["callback_data"]
            for row in keyboard
            for button in row
            if "callback_data" in button
        ]
        disabled = [button for row in keyboard for button in row if "disabled" in button]
        self.assertIn("checkpoint غير موجود", text)
        self.assertFalse(any(value.startswith("resume:") for value in callbacks))
        self.assertEqual(disabled, [{"text": "⛔ استئناف غير متاح", "disabled": {}}])
        self.assertIn("restart:req-history", callbacks)

    def test_restart_creates_new_request_and_requires_normal_confirmation(self):
        request = _request()
        state = control.default_state()
        state["requests"][request["request_id"]] = request
        restarted = control.restart_request_from_history(state, request["request_id"])
        self.assertNotEqual(restarted["request_id"], request["request_id"])
        self.assertNotEqual(restarted["request_sha256"], request["request_sha256"])
        self.assertEqual(restarted["status"], "awaiting_confirmation")
        self.assertIsNone(restarted["dispatched_at"])
        self.assertEqual(state["current_request_id"], restarted["request_id"])

    def test_resume_reruns_same_original_run_without_new_dispatch(self):
        request = _request()
        state = control.default_state()
        state["requests"][request["request_id"]] = request
        decision = {
            "available": True,
            "reason": "",
            "completed_stage": "voice",
            "stage_label": "Voice",
            "run_id": "42",
        }
        with mock.patch.object(control, "_resume_decision_for_request", return_value=decision), mock.patch.object(
            control.resume_history, "rerun_workflow"
        ) as rerun:
            result = control.request_resume_rerun(state, request["request_id"])
        self.assertTrue(result["available"])
        rerun.assert_called_once_with("42")
        self.assertEqual(request["production"]["run_id"], "42")
        self.assertEqual(request["production"]["last_job_status"], "rerun_requested")

    def test_resume_callback_never_creates_dispatch_file(self):
        request = _request()
        state = control.default_state()
        state["requests"][request["request_id"]] = request
        update = {
            "callback_query": {
                "from": {"id": 123},
                "message": {"chat": {"id": 123}},
                "data": "resume:req-history",
            }
        }
        decision = {
            "available": True,
            "reason": "",
            "completed_stage": "voice",
            "stage_label": "Voice",
            "run_id": "42",
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            "os.environ", {"TELEGRAM_CHAT_ID": "123"}, clear=False
        ), mock.patch.object(control, "_resume_decision_for_request", return_value=decision), mock.patch.object(
            control.resume_history, "rerun_workflow"
        ), mock.patch.object(control, "send_telegram"):
            dispatch = Path(tmp) / "dispatch.json"
            control.handle_update(state, update, dispatch)
            self.assertFalse(dispatch.exists())


class TelegramResumeHistoryWorkflowContractTests(unittest.TestCase):
    def test_production_workflow_records_exact_run_and_durable_checkpoint_stage(self):
        text = Path(".github/workflows/clean-v2-telegram-production.yml").read_text(encoding="utf-8")
        self.assertIn("record-production-start", text)
        self.assertIn("--run-id \"$GITHUB_RUN_ID\"", text)
        self.assertIn("--run-attempt \"$GITHUB_RUN_ATTEMPT\"", text)
        self.assertIn("CLEAN_V2_RESUME_CACHE_KEY", text)
        self.assertIn("telegram_resume_history.py annotate-manifest", text)
        self.assertIn("--checkpoint \"$CLEAN_V2_RESUME/$fmt/resume-checkpoint.json\"", text)
        self.assertIn("record-production-terminal", text)
        self.assertIn("steps.upload.outputs.artifact-id", text)

    def test_control_and_production_use_same_engine_pin(self):
        control_workflow = Path(".github/workflows/telegram-clean-v2-control.yml").read_text(encoding="utf-8")
        production_workflow = Path(".github/workflows/clean-v2-telegram-production.yml").read_text(encoding="utf-8")
        marker = "ISCO_ENGINE_SHA: 3cbd689819e6b0e0b2ea9904d1998e24a5e2a293"
        self.assertIn(marker, control_workflow)
        self.assertIn(marker, production_workflow)
        self.assertIn("CURRENT_RUNNER_SHA=$(git rev-parse HEAD)", control_workflow)


if __name__ == "__main__":
    unittest.main()
