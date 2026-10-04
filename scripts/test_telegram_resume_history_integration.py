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
    def test_saved_root_restores_long_short_podcast_tabs(self):
        state = control.default_state()

        long_request = _request()
        long_request["request_id"] = "req-long"
        long_request["scope"] = "long"
        long_request["approved_topic"] = "موضوع طويل"
        long_request["selected_at"] = "2026-10-01T01:00:00Z"

        short_old = _request()
        short_old["request_id"] = "req-short-old"
        short_old["approved_topic"] = "لماذا تجعلنا كثرة الخيارات أقل حسمًا؟"
        short_old["selected_at"] = "2026-10-01T01:00:00Z"

        short_new = _request()
        short_new["request_id"] = "req-short-new"
        short_new["approved_topic"] = "لماذا تجعلنا كثرة الخيارات أقل حسمًا؟"
        short_new["selected_at"] = "2026-10-02T01:00:00Z"

        podcast_request = _request()
        podcast_request["request_id"] = "req-podcast"
        podcast_request["scope"] = "podcast"
        podcast_request["approved_topic"] = "موضوع بودكاست"
        podcast_request["selected_at"] = "2026-10-03T01:00:00Z"

        state["requests"] = {
            item["request_id"]: item
            for item in (long_request, short_old, short_new, podcast_request)
        }

        with mock.patch.object(control, "_release_library_records", return_value=[]):
            text, keyboard = control._history_view(state)

        callbacks = [button["callback_data"] for row in keyboard for button in row]
        self.assertEqual(
            callbacks[:3],
            ["historyscope:long", "historyscope:short", "historyscope:podcast"],
        )
        self.assertIn("🎬 طويل — 1", text)
        self.assertIn("⚡ شورت — 1", text)
        self.assertIn("🎙️ بودكاست — 1", text)
        self.assertFalse(any(value.startswith("history:") for value in callbacks))

    def test_history_scope_shows_only_kind_and_dedupes_to_newest_request(self):
        state = control.default_state()

        old = _request()
        old["request_id"] = "req-old"
        old["approved_topic"] = "لماذا تجعلنا كثرة الخيارات أقل حسمًا؟"
        old["selected_at"] = "2026-10-01T01:00:00Z"

        new = _request()
        new["request_id"] = "req-new"
        new["approved_topic"] = "لماذا تجعلنا كثرة الخيارات أقل حسمًا؟"
        new["selected_at"] = "2026-10-02T01:00:00Z"

        other = _request()
        other["request_id"] = "req-long"
        other["scope"] = "long"
        other["approved_topic"] = "موضوع طويل لا يجب أن يظهر"
        other["selected_at"] = "2026-10-03T01:00:00Z"

        state["requests"] = {
            item["request_id"]: item
            for item in (old, new, other)
        }

        with mock.patch.object(control, "_release_library_records", return_value=[]):
            text, keyboard = control._history_scope_view(state, "short")

        callbacks = [button["callback_data"] for row in keyboard for button in row]
        self.assertIn("history:req-new", callbacks)
        self.assertNotIn("history:req-old", callbacks)
        self.assertNotIn("history:req-long", callbacks)
        self.assertEqual(callbacks.count("history:req-new"), 1)
        self.assertNotIn("موضوع طويل لا يجب أن يظهر", text)
        self.assertEqual(keyboard[-1][0]["callback_data"], "main:saved")

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
        self.assertIn("historyscope:short", callbacks)

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
        self.assertEqual(disabled, [])
        self.assertIn("restart:req-history", callbacks)
        restart_button = next(
            button for row in keyboard for button in row
            if button.get("callback_data") == "restart:req-history"
        )
        self.assertIn("إعادة المحاولة", restart_button["text"])

    def test_history_scope_callback_opens_only_requested_tab(self):
        request = _request()
        state = control.default_state()
        state["requests"][request["request_id"]] = request
        update = {
            "callback_query": {
                "from": {"id": 123},
                "message": {"chat": {"id": 123}},
                "data": "historyscope:short",
            }
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            "os.environ", {"TELEGRAM_CHAT_ID": "123"}, clear=False
        ), mock.patch.object(control, "_release_library_records", return_value=[]), mock.patch.object(
            control, "send_telegram"
        ) as send:
            control.handle_update(state, update, Path(tmp) / "dispatch.json")

        text, keyboard = send.call_args.args
        self.assertIn("📚 المحفوظات — ⚡ شورت", text)
        callbacks = [button["callback_data"] for row in keyboard for button in row]
        self.assertIn("history:req-history", callbacks)
        self.assertEqual(keyboard[-1][0]["callback_data"], "main:saved")

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
        marker = "ISCO_ENGINE_SHA: 595f69a28ff6ddc5532291c9f113f334562292f9"
        self.assertIn(marker, control_workflow)
        self.assertIn(marker, production_workflow)
        self.assertIn("CURRENT_RUNNER_SHA=$(git rev-parse HEAD)", control_workflow)


if __name__ == "__main__":
    unittest.main()
