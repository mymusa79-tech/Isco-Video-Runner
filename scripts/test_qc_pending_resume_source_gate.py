from __future__ import annotations

import io
import json
import unittest
import urllib.error
from typing import Any

from scripts.qc_pending_resume_source_gate import verify_historical_resume_source


SHA = "a" * 40
RUN_ID = "99123"
REPO = "owner/repo"


class _Response:
    def __init__(self, payload: dict[str, Any]):
        self._buffer = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def read(self) -> bytes:
        return self._buffer.read()


class _Opener:
    def __init__(self, *, source_sha: str = SHA, source_path: str = ".github/workflows/produce-resilient-v4.yml", source_conclusion: str = "failure", compare_status: str = "ahead", protected: bool = True, bad_tag: bool = False, omit_stage: bool = False):
        self.source_sha = source_sha
        self.source_path = source_path
        self.source_conclusion = source_conclusion
        self.compare_status = compare_status
        self.protected = protected
        self.bad_tag = bad_tag
        self.omit_stage = omit_stage

    def __call__(self, request, timeout=20):
        del timeout
        url = request.full_url
        if url.endswith(f"/actions/runs/{RUN_ID}"):
            return _Response({
                "id": int(RUN_ID),
                "name": "Isco Video Production Resilient V4",
                "path": self.source_path,
                "head_sha": self.source_sha,
                "head_branch": "main",
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": self.source_conclusion,
            })
        if url.endswith("/branches/main"):
            return _Response({"protected": self.protected, "commit": {"sha": "c" * 40}})
        if f"/compare/{SHA}...main" in url:
            return _Response({"status": self.compare_status})
        if "/git/ref/tags/" in url:
            return _Response({"object": {"type": "commit", "sha": "b" * 40 if self.bad_tag else SHA}})
        if "/actions/runs?" in url:
            runs = [
                {
                    "id": 101,
                    "name": "Verify Private Engine",
                    "path": ".github/workflows/verify-private-engine.yml",
                    "head_sha": SHA,
                    "head_branch": "main",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "success",
                },
                {
                    "id": 102,
                    "name": "Verify Production Stage Ladder",
                    "path": ".github/workflows/verify-production-stage-ladder.yml",
                    "head_sha": SHA,
                    "head_branch": "main",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "success",
                },
            ]
            if self.omit_stage:
                runs = runs[:1]
            return _Response({"workflow_runs": runs})
        raise urllib.error.URLError(f"unexpected URL {url}")


class QCPendingResumeSourceGateTests(unittest.TestCase):
    def test_accepts_historical_certified_main_source_after_main_advances(self) -> None:
        result = verify_historical_resume_source(
            repository=REPO,
            source_run_id=RUN_ID,
            source_runner_sha=SHA,
            token="token",
            opener=_Opener(),
        )
        self.assertEqual(result["status"], "green")
        self.assertTrue(result["source_was_certified_protected_main"])
        self.assertTrue(result["source_is_ancestor_of_current_main"])
        self.assertEqual(result["source_run_id"], RUN_ID)
        self.assertFalse(result["production_dispatch_performed"])

    def test_rejects_noncanonical_source_workflow(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "not canonical Production V4"):
            verify_historical_resume_source(
                repository=REPO,
                source_run_id=RUN_ID,
                source_runner_sha=SHA,
                token="token",
                opener=_Opener(source_path=".github/workflows/other.yml"),
            )

    def test_rejects_source_run_sha_mismatch(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "run/head SHA mismatch"):
            verify_historical_resume_source(
                repository=REPO,
                source_run_id=RUN_ID,
                source_runner_sha=SHA,
                token="token",
                opener=_Opener(source_sha="b" * 40),
            )

    def test_rejects_source_not_ancestor_of_current_main(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "not an ancestor"):
            verify_historical_resume_source(
                repository=REPO,
                source_run_id=RUN_ID,
                source_runner_sha=SHA,
                token="token",
                opener=_Opener(compare_status="diverged"),
            )

    def test_rejects_uncertified_historical_sha(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "certification ref"):
            verify_historical_resume_source(
                repository=REPO,
                source_run_id=RUN_ID,
                source_runner_sha=SHA,
                token="token",
                opener=_Opener(bad_tag=True),
            )

    def test_rejects_missing_stage_ladder_receipt(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Verify Production Stage Ladder"):
            verify_historical_resume_source(
                repository=REPO,
                source_run_id=RUN_ID,
                source_runner_sha=SHA,
                token="token",
                opener=_Opener(omit_stage=True),
            )


if __name__ == "__main__":
    unittest.main()