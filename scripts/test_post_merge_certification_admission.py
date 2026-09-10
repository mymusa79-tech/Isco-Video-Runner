from __future__ import annotations

import io
import json
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.production_certification_readiness import (
    inspect_production_certification_readiness,
    wait_for_production_certification_readiness,
)
from scripts.telegram_certification_resume import reserved_dispatch_for_runner


SHA = "a" * 40
NEW_SHA = "b" * 40
REPO = "owner/repo"


class _Response:
    def __init__(self, payload: dict[str, Any]):
        self._buffer = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return self._buffer.read()


def _run(run_id: int, name: str, path: str, *, status: str = "completed", conclusion: str | None = "success") -> dict[str, Any]:
    return {
        "id": run_id,
        "name": name,
        "path": path,
        "head_sha": SHA,
        "head_branch": "main",
        "event": "push",
        "status": status,
        "conclusion": conclusion,
    }


class _Opener:
    def __init__(
        self,
        *,
        branch_sha: str = SHA,
        private_status: str = "completed",
        private_conclusion: str | None = "success",
        stage_status: str = "completed",
        stage_conclusion: str | None = "success",
        missing_tags: set[str] | None = None,
        bad_tag: str | None = None,
        omit_workflows: bool = False,
    ):
        self.branch_sha = branch_sha
        self.private_status = private_status
        self.private_conclusion = private_conclusion
        self.stage_status = stage_status
        self.stage_conclusion = stage_conclusion
        self.missing_tags = set(missing_tags or set())
        self.bad_tag = bad_tag
        self.omit_workflows = omit_workflows

    def __call__(self, request, timeout=20):
        del timeout
        url = request.full_url
        if url.endswith("/branches/main"):
            return _Response({"protected": True, "commit": {"sha": self.branch_sha}})
        marker = "/git/ref/tags/"
        if marker in url:
            tag = url.split(marker, 1)[1]
            if tag in self.missing_tags:
                raise urllib.error.HTTPError(url, 404, "not found", hdrs=None, fp=None)
            if self.bad_tag == tag:
                return _Response({"object": {"type": "commit", "sha": NEW_SHA}})
            return _Response({"object": {"type": "commit", "sha": SHA}})
        if "/actions/runs?" in url:
            runs = []
            if not self.omit_workflows:
                runs = [
                    _run(101, "Verify Private Engine", ".github/workflows/verify-private-engine.yml", status=self.private_status, conclusion=self.private_conclusion),
                    _run(102, "Verify Production Stage Ladder", ".github/workflows/verify-production-stage-ladder.yml", status=self.stage_status, conclusion=self.stage_conclusion),
                ]
            return _Response({"workflow_runs": runs})
        raise urllib.error.URLError(f"unexpected URL {url}")


class CertificationReadinessTests(unittest.TestCase):
    def inspect(self, opener: _Opener) -> dict[str, Any]:
        return inspect_production_certification_readiness(repository=REPO, runner_sha=SHA, git_ref="refs/heads/main", token="token", opener=opener)

    def test_ready_requires_exact_tags_and_successful_main_push_runs(self) -> None:
        result = self.inspect(_Opener())
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["reason"], "exact_sha_certified")
        self.assertFalse(result["production_dispatch_performed"])

    def test_missing_tag_while_canonical_run_is_active_is_pending(self) -> None:
        result = self.inspect(_Opener(private_status="in_progress", private_conclusion=None, missing_tags={f"full-regression-green-{SHA}"}))
        self.assertEqual(result["status"], "pending")

    def test_no_visible_canonical_runs_is_pending_not_failed(self) -> None:
        result = self.inspect(_Opener(omit_workflows=True, missing_tags={f"full-regression-green-{SHA}", f"stage-ladder-green-{SHA}"}))
        self.assertEqual(result["status"], "pending")

    def test_failed_canonical_run_is_terminal_failed(self) -> None:
        result = self.inspect(_Opener(private_status="completed", private_conclusion="failure", missing_tags={f"full-regression-green-{SHA}"}))
        self.assertEqual(result["status"], "failed")

    def test_missing_tag_after_all_canonical_runs_succeed_is_failed(self) -> None:
        result = self.inspect(_Opener(missing_tags={f"full-regression-green-{SHA}"}))
        self.assertEqual(result["status"], "failed")

    def test_mismatched_tag_identity_is_failed(self) -> None:
        result = self.inspect(_Opener(bad_tag=f"stage-ladder-green-{SHA}"))
        self.assertEqual(result["status"], "failed")

    def test_main_advance_makes_reserved_sha_stale(self) -> None:
        result = self.inspect(_Opener(branch_sha=NEW_SHA))
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["reason"], "main_advanced")

    def test_bounded_wait_transitions_pending_to_ready(self) -> None:
        openers = [_Opener(private_status="in_progress", private_conclusion=None, missing_tags={f"full-regression-green-{SHA}"}), _Opener()]
        calls = {"n": 0}
        sleeps: list[float] = []

        def opener(request, timeout=20):
            index = min(calls["n"], 1)
            if "/actions/runs?" in request.full_url:
                calls["n"] += 1
            return openers[index](request, timeout=timeout)

        result = wait_for_production_certification_readiness(repository=REPO, runner_sha=SHA, git_ref="refs/heads/main", token="token", max_attempts=2, poll_seconds=0.25, opener=opener, sleeper=sleeps.append)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(sleeps, [0.25])

    def test_bounded_wait_exhaustion_remains_pending(self) -> None:
        result = wait_for_production_certification_readiness(repository=REPO, runner_sha=SHA, git_ref="refs/heads/main", token="token", max_attempts=2, poll_seconds=0, opener=_Opener(private_status="in_progress", private_conclusion=None, missing_tags={f"full-regression-green-{SHA}"}), sleeper=lambda _: None)
        self.assertEqual(result["status"], "pending")
        self.assertTrue(result["wait_exhausted"])


class CertificationResumeSelectionTests(unittest.TestCase):
    def _reserved(self, *, runner_sha: str = SHA) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        return {
            "schema_version": 1,
            "request_id": "req-1",
            "request_sha256": "c" * 64,
            "authorization_id": "d" * 32,
            "status": "dispatch_reserved",
            "requested_at": now,
            "reserved_at": now,
            "runner_sha": runner_sha,
            "attempt": 1,
        }

    def test_exact_live_reservation_is_selected_for_certification_resume(self) -> None:
        item = self._reserved()
        result = reserved_dispatch_for_runner({"production_queue": [item]}, SHA)
        self.assertIs(result, item)

    def test_reservation_for_other_sha_is_not_resumed(self) -> None:
        result = reserved_dispatch_for_runner({"production_queue": [self._reserved(runner_sha=NEW_SHA)]}, SHA)
        self.assertIsNone(result)

    def test_multiple_live_reservations_for_same_sha_fail_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Multiple live Telegram reservations"):
            reserved_dispatch_for_runner({"production_queue": [self._reserved(), self._reserved()]}, SHA)


class WorkflowCompositionTests(unittest.TestCase):
    def test_gateway_never_dispatches_v4_before_certification_ready(self) -> None:
        text = Path(".github/workflows/telegram-production-request.yml").read_text(encoding="utf-8")
        wait_at = text.index("Wait for exact-SHA production certification readiness")
        dispatch_at = text.index("Dispatch exact reservation to the single V4 owner")
        self.assertLess(wait_at, dispatch_at)
        self.assertIn("steps.certification.outputs.ready == 'true' && steps.capacity.outputs.available == 'true'", text)
        self.assertIn("steps.certification.outputs.pending != 'true'", text)

    def test_event_driven_resume_is_bound_to_main_push_certification(self) -> None:
        text = Path(".github/workflows/resume-telegram-production-after-certification.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_run:", text)
        self.assertIn("Verify Private Engine", text)
        self.assertIn("Verify Production Stage Ladder", text)
        self.assertIn("github.event.workflow_run.event == 'push'", text)
        self.assertIn("github.event.workflow_run.head_branch == 'main'", text)
        self.assertIn("SOURCE_RUNNER_SHA: ${{ github.event.workflow_run.head_sha }}", text)
        self.assertIn("steps.readiness.outputs.status == 'ready'", text)


if __name__ == "__main__":
    unittest.main()
