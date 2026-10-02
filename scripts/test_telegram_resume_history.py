from __future__ import annotations

import io
import json
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts import telegram_resume_history as history


def make_request() -> dict:
    return {
        "request_id": "req-1",
        "request_sha256": "hash-1",
        "approved_topic": "موضوع غير مكتمل",
        "scope": "short",
        "selected_at": "2026-10-01T10:00:00Z",
        "status": "dispatched",
    }


def artifact_zip(manifest: dict) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("req-1/short/run-manifest.json", json.dumps(manifest))
    return stream.getvalue()


class ResumeHistoryTests(unittest.TestCase):
    def test_incomplete_requests_excludes_final_published(self):
        a = make_request()
        b = {**make_request(), "request_id": "req-2", "approved_topic": "منشور"}
        b["production"] = {"final_published": True}
        state = {"requests": {"req-1": a, "req-2": b}}
        self.assertEqual([x["request_id"] for x in history.incomplete_requests(state)], ["req-1"])

    def test_record_start_keeps_one_original_run_across_reruns(self):
        state = {"requests": {"req-1": make_request()}}
        history.record_production_start(
            state,
            request_id="req-1",
            request_sha256="hash-1",
            run_id="42",
            run_attempt="1",
            run_url="https://github.test/runs/42",
            runner_sha="runner",
            engine_sha="engine",
            resume_cache_key="cache-1",
            at="2026-10-01T10:01:00Z",
        )
        history.record_production_start(
            state,
            request_id="req-1",
            request_sha256="hash-1",
            run_id="42",
            run_attempt="2",
            run_url="https://github.test/runs/42",
            runner_sha="runner",
            engine_sha="engine",
            resume_cache_key="cache-2",
            at="2026-10-02T10:01:00Z",
        )
        self.assertEqual(state["requests"]["req-1"]["production"]["run_id"], "42")
        self.assertEqual(state["requests"]["req-1"]["production"]["run_attempt"], 2)
        with self.assertRaisesRegex(RuntimeError, "different GitHub run"):
            history.record_production_start(
                state,
                request_id="req-1",
                request_sha256="hash-1",
                run_id="99",
                run_attempt="1",
                run_url="",
                runner_sha="runner",
                engine_sha="engine",
                resume_cache_key="cache-99",
            )

    def test_manifest_annotation_uses_actual_prepared_checkpoint(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "run-manifest.json"
            checkpoint = root / "resume-checkpoint.json"
            manifest.write_text(json.dumps({"runner_sha": "runner", "engine_sha": "engine"}), encoding="utf-8")
            checkpoint.write_text(json.dumps({"completed_stage": "text_audit"}), encoding="utf-8")
            result = history.annotate_manifest(manifest, checkpoint, cache_key="cache", save_allowed=True)
            self.assertEqual(result["completed_stage"], "text_audit")
            stored = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertTrue(stored["durable_resume"]["save_allowed"])

    def test_resume_enabled_only_with_exact_run_cache_and_artifact_manifest(self):
        request = make_request()
        request["production"] = {
            "run_id": "42",
            "runner_sha": "runner",
            "engine_sha": "engine",
            "resume_cache_key": "cache-42",
            "artifact_id": 777,
            "final_published": False,
        }
        manifest = {
            "runner_sha": "runner",
            "engine_sha": "engine",
            "durable_resume": {
                "schema_version": 1,
                "save_allowed": True,
                "cache_key": "cache-42",
                "completed_stage": "text_audit",
            },
        }

        def fake_json(path: str):
            if path == "actions/runs/42":
                return {
                    "id": 42,
                    "status": "completed",
                    "head_sha": "runner",
                    "created_at": "2026-10-01T00:00:00Z",
                }
            if path.startswith("actions/caches?"):
                return {"actions_caches": [{"key": "cache-42", "ref": "refs/heads/main"}]}
            if path == "actions/artifacts/777":
                return {"id": 777, "name": "clean-v2-telegram-1", "expired": False}
            raise AssertionError(path)

        decision = history.evaluate_resume(
            request,
            current_runner_sha="runner",
            current_engine_sha="engine",
            github_json=fake_json,
            github_bytes=lambda path: artifact_zip(manifest),
            now=datetime(2026, 10, 2, tzinfo=timezone.utc),
        )
        self.assertTrue(decision["available"])
        self.assertEqual(decision["completed_stage"], "text_audit")
        self.assertEqual(decision["stage_label"], "Text Audit")

    def test_runner_sha_change_disables_without_api_calls(self):
        request = make_request()
        request["production"] = {
            "run_id": "42",
            "runner_sha": "old-runner",
            "engine_sha": "engine",
            "resume_cache_key": "cache-42",
        }
        decision = history.evaluate_resume(
            request,
            current_runner_sha="new-runner",
            current_engine_sha="engine",
            github_json=lambda path: self.fail(path),
            github_bytes=lambda path: self.fail(path),
        )
        self.assertFalse(decision["available"])
        self.assertIn("Runner SHA", decision["reason"])

    def test_missing_cache_disables_with_specific_reason(self):
        request = make_request()
        request["production"] = {
            "run_id": "42",
            "runner_sha": "runner",
            "engine_sha": "engine",
            "resume_cache_key": "cache-42",
        }

        def fake_json(path: str):
            if path == "actions/runs/42":
                return {
                    "status": "completed",
                    "head_sha": "runner",
                    "created_at": "2026-10-01T00:00:00Z",
                }
            if path.startswith("actions/caches?"):
                return {"actions_caches": []}
            raise AssertionError(path)

        decision = history.evaluate_resume(
            request,
            current_runner_sha="runner",
            current_engine_sha="engine",
            github_json=fake_json,
            github_bytes=lambda path: self.fail(path),
            now=datetime(2026, 10, 2, tzinfo=timezone.utc),
        )
        self.assertFalse(decision["available"])
        self.assertIn("checkpoint", decision["reason"])


if __name__ == "__main__":
    unittest.main()
