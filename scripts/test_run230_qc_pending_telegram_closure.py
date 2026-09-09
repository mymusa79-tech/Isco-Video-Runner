from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import telegram_final_notify as notify
from scripts import telegram_gold_resume_control as gold
from scripts import telegram_production_queue as production
from scripts import telegram_v4_ingress as ingress
from scripts.gold_enforce_phase4 import QC_PENDING_DIAGNOSTIC_BUNDLE_DIRNAME

RUNNER_SHA = "a" * 40
ENGINE_SHA = "b" * 40
FINAL_SHA = "c" * 64
REQUEST_ID = "req-6312bcca7a93"
AUTHORIZATION_ID = "1" * 32


def _request() -> dict:
    item = {
        "schema_version": 1,
        "request_id": REQUEST_ID,
        "source": "telegram_editorial_control_panel",
        "kind": "short",
        "approval_scope": "short_only",
        "approved_topic": "Run 230 regression",
        "approved_at": "2026-09-08T22:30:00+00:00",
        "approved_by_user": True,
        "production_dispatch_authorized": False,
        "status": "approved_waiting_production_activation",
    }
    item["request_sha256"] = production._request_hash(item)
    return item


def _state() -> tuple[dict, dict]:
    request = _request()
    entry = {
        "schema_version": 1,
        "request_id": REQUEST_ID,
        "request_sha256": request["request_sha256"],
        "authorization_id": AUTHORIZATION_ID,
        "kind": "short",
        "approval_scope": "short_only",
        "status": "dispatch_consumed",
        "runner_sha": RUNNER_SHA,
        "workflow_run_id": "34286657157",
    }
    return {
        "requests": {REQUEST_ID: request},
        "production_queue": [entry],
        "gold_resume_queue": [],
    }, request


def _checkpoint() -> dict:
    return {
        "schema_version": 2,
        "contract_id": "gold.qc-pending.v1",
        "status": "GOLD_VISION_PENDING_PROVIDER_CAPACITY",
        "release_allowed": False,
        "resumable": True,
        "failure_taxonomy": "VisionProviderMeshUnavailableError",
        "runner_sha": RUNNER_SHA,
        "engine_sha": ENGINE_SHA,
        "source_run_id": "34286657157",
        "source_run_attempt": "1",
        "format": "moment",
        "final": {"sha256": FINAL_SHA},
    }


class Run230QCPendingTelegramClosureTests(unittest.TestCase):
    def _state_file(self) -> tuple[Path, dict, dict]:
        root = Path(tempfile.mkdtemp(prefix="run230-telegram-"))
        path = root / "state.json"
        state, request = _state()
        path.write_text(json.dumps(state), encoding="utf-8")
        return path, state, request

    def test_exact_gold_capacity_pause_becomes_qc_pending_not_failed(self) -> None:
        path, _state_before, request = self._state_file()
        with patch.dict(
            os.environ,
            {
                "GITHUB_SHA": RUNNER_SHA,
                "GITHUB_RUN_ID": "34286657157",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_RUN_NUMBER": "230",
            },
            clear=False,
        ), patch.object(
            ingress,
            "_latest_current_qc_pending",
            return_value=(Path("qc-pending.json"), _checkpoint(), Path(QC_PENDING_DIAGNOSTIC_BUNDLE_DIRNAME)),
        ):
            ingress.fail(
                state_path=path,
                request_id=REQUEST_ID,
                request_sha256=request["request_sha256"],
                authorization_id=AUTHORIZATION_ID,
                reason="production_failed",
            )

        state = json.loads(path.read_text(encoding="utf-8"))
        entry = state["production_queue"][0]
        self.assertEqual(entry["status"], "qc_pending")
        self.assertEqual(entry["failure_reason"], "gold_vision_provider_capacity")
        self.assertEqual(entry["qc_pending"]["source_run_id"], "34286657157")
        self.assertEqual(entry["qc_pending"]["artifact_name"], "isco-resilient-v4-diagnostics-230")
        self.assertEqual(entry["qc_pending"]["final_sha256"], FINAL_SHA)

        status, action = gold.enqueue_gold_resume(state, REQUEST_ID, chat_id=77)
        self.assertEqual(status, "requested")
        self.assertEqual(action["artifact_name"], "isco-resilient-v4-diagnostics-230")
        self.assertEqual(action["final_sha256"], FINAL_SHA)

    def test_missing_or_invalid_recovery_evidence_remains_generic_failure(self) -> None:
        path, _state_before, request = self._state_file()
        with patch.object(ingress, "_latest_current_qc_pending", return_value=None):
            ingress.fail(
                state_path=path,
                request_id=REQUEST_ID,
                request_sha256=request["request_sha256"],
                authorization_id=AUTHORIZATION_ID,
                reason="production_failed",
            )
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(state["production_queue"][0]["status"], "failed")
        self.assertNotIn("qc_pending", state["production_queue"][0])

    def test_non_production_failure_never_promotes_to_qc_pending(self) -> None:
        path, _state_before, request = self._state_file()
        with patch.object(
            ingress,
            "_latest_current_qc_pending",
            side_effect=AssertionError("must not inspect Gold checkpoint"),
        ):
            ingress.fail(
                state_path=path,
                request_id=REQUEST_ID,
                request_sha256=request["request_sha256"],
                authorization_id=AUTHORIZATION_ID,
                reason="production_cancelled",
            )
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(state["production_queue"][0]["status"], "failed")
        self.assertEqual(state["production_queue"][0]["failure_reason"], "production_cancelled")

    def test_qc_pending_is_terminal_against_generic_failure_overwrite(self) -> None:
        state, request = _state()
        from scripts.telegram_qc_pending_bridge_v1 import mark_dispatch_qc_pending_diagnostics

        mark_dispatch_qc_pending_diagnostics(
            state,
            REQUEST_ID,
            request["request_sha256"],
            AUTHORIZATION_ID,
            source_run_id="34286657157",
            source_run_attempt="1",
            artifact_name="isco-resilient-v4-diagnostics-230",
            runner_sha=RUNNER_SHA,
            engine_sha=ENGINE_SHA,
            final_sha256=FINAL_SHA,
            fmt="moment",
        )
        with self.assertRaisesRegex(RuntimeError, "cannot transition back to failed"):
            production.mark_dispatch_failed(
                state,
                REQUEST_ID,
                request["request_sha256"],
                AUTHORIZATION_ID,
                reason="production_failed",
            )
        self.assertEqual(state["production_queue"][0]["status"], "qc_pending")

    def test_terminal_card_exposes_existing_goldresume_callback_only_for_pending(self) -> None:
        keyboard = notify.terminal_keyboard(
            job_status="qc_pending",
            run_url="https://github.com/example/run",
            run_id="34286657157",
            progress_message_id="965",
            request_id=REQUEST_ID,
        )
        rows = keyboard["inline_keyboard"]
        callbacks = [button.get("callback_data") for row in rows for button in row if "callback_data" in button]
        self.assertIn(f"cmd:goldresume-{REQUEST_ID}", callbacks)

        failed = notify.terminal_keyboard(
            job_status="failure",
            run_url="https://github.com/example/run",
            run_id="34286657157",
            progress_message_id="965",
            request_id=REQUEST_ID,
        )
        failed_callbacks = [
            button.get("callback_data")
            for row in failed["inline_keyboard"]
            for button in row
            if "callback_data" in button
        ]
        self.assertNotIn(f"cmd:goldresume-{REQUEST_ID}", failed_callbacks)

    def test_source_diagnostics_transport_contains_exact_resume_bundle(self) -> None:
        workflow = Path(".github/workflows/produce-resilient-v4.yml").read_text(encoding="utf-8")
        resume = Path(".github/workflows/resume-gold-qc-pending.yml").read_text(encoding="utf-8")
        self.assertTrue(QC_PENDING_DIAGNOSTIC_BUNDLE_DIRNAME.startswith("short-"))
        self.assertIn("engine/output/*/short-*", workflow)
        self.assertIn("name: isco-resilient-v4-diagnostics-${{ github.run_number }}", workflow)
        self.assertIn("-name resume-manifest.json", resume)


if __name__ == "__main__":
    unittest.main()
