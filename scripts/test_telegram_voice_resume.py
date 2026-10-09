import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from scripts import telegram_clean_v2_control as control
from scripts import telegram_resume_history as history

ENGINE = "e" * 40
SHA = "a" * 64


def _request(**production):
    base = {"run_id": "123", "engine_sha": ENGINE, "final_published": False}
    base.update(production)
    return {"request_id": "req-1", "request_sha256": SHA, "status": "dispatched", "production": base}


def _gh(run_status="completed", age_days=1, cache_key=None):
    created = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat().replace("+00:00", "Z")
    key = cache_key if cache_key is not None else f"clean-v2-voice-bank-v1-telegram-Linux-{ENGINE}-{SHA}-123-1"

    def getter(path):
        if path.startswith("actions/runs/"):
            return {"status": run_status, "created_at": created}
        if path.startswith("actions/caches"):
            return {"actions_caches": [{"key": key, "ref": "refs/heads/main"}] if key else []}
        raise AssertionError(path)

    return getter


class VoiceResumeDecisionTests(unittest.TestCase):
    def test_available_even_when_runner_sha_differs(self):
        out = history.evaluate_voice_resume(_request(runner_sha="old"), current_engine_sha=ENGINE, github_json=_gh())
        self.assertTrue(out["available"])

    def test_blocked_when_engine_changed_old_or_running_or_no_cache(self):
        cases = [
            (_request(engine_sha="x" * 40), _gh()),
            (_request(), _gh(age_days=7)),
            (_request(), _gh(run_status="in_progress")),
            (_request(), _gh(cache_key="")),
            (_request(final_published=True), _gh()),
        ]
        for req, getter in cases:
            out = history.evaluate_voice_resume(req, current_engine_sha=ENGINE, github_json=getter)
            self.assertFalse(out["available"])


class StageVoiceDispatchTests(unittest.TestCase):
    def test_stages_resume_flag_for_same_immutable_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.json"
            req = _request()
            req["approved_topic"] = "t"
            state = {"requests": {"req-1": req}}
            with mock.patch.object(control, "_voice_resume_decision_for_request", return_value={"available": True}):
                control.stage_voice_resume_dispatch(state, "req-1", path)
            payload = json.loads(path.read_text())
            self.assertEqual(payload, {"request_id": "req-1", "request_sha256": SHA, "resume_from_voice": True})

    def test_nothing_staged_when_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.json"
            state = {"requests": {"req-1": _request()}}
            with mock.patch.object(control, "_voice_resume_decision_for_request", return_value={"available": False, "reason": "x"}):
                out = control.stage_voice_resume_dispatch(state, "req-1", path)
            self.assertFalse(out["available"])
            self.assertFalse(path.exists())


class WorkflowPayloadTests(unittest.TestCase):
    JQ = (
        "jq -nc --arg ref main --arg request_id R --arg request_sha256 S --arg resume_voice \"$V\" "
        "'{ref:$ref,inputs:({request_id:$request_id,request_sha256:$request_sha256} + "
        "(if $resume_voice == \"true\" then {resume_from_voice:\"true\"} else {} end))}'"
    )

    def test_workflow_contains_resume_input_expression(self):
        text = Path(".github/workflows/telegram-clean-v2-control.yml").read_text()
        self.assertIn("resume_from_voice", text)
        self.assertIn("if .resume_from_voice == true", text)


if __name__ == "__main__":
    unittest.main()
