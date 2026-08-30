from __future__ import annotations

import io
import json
import unittest
import urllib.error
from typing import Any

from scripts.production_certification_gate import verify_certified_production_source


SHA = "a" * 40
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


def _run(
    *,
    run_id: int,
    name: str,
    path: str,
    event: str = "push",
    conclusion: str = "success",
    head_sha: str = SHA,
    head_branch: str = "main",
) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": name,
        "path": path,
        "head_sha": head_sha,
        "head_branch": head_branch,
        "event": event,
        "status": "completed",
        "conclusion": conclusion,
    }


class _Opener:
    def __init__(
        self,
        *,
        protected: bool = True,
        branch_sha: str = SHA,
        bad_tag: str | None = None,
        private_event: str = "push",
        stage_conclusion: str = "success",
        omit_workflow: str | None = None,
    ):
        self.protected = protected
        self.branch_sha = branch_sha
        self.bad_tag = bad_tag
        self.private_event = private_event
        self.stage_conclusion = stage_conclusion
        self.omit_workflow = omit_workflow
        self.urls: list[str] = []

    def __call__(self, request, timeout=20):
        del timeout
        url = request.full_url
        self.urls.append(url)
        if url.endswith("/branches/main"):
            return _Response({"protected": self.protected, "commit": {"sha": self.branch_sha}})
        marker = "/git/ref/tags/"
        if marker in url:
            tag = url.split(marker, 1)[1]
            if self.bad_tag and tag == self.bad_tag:
                return _Response({"object": {"type": "commit", "sha": "b" * 40}})
            return _Response({"object": {"type": "commit", "sha": SHA}})
        if "/actions/runs?" in url:
            runs = [
                _run(
                    run_id=101,
                    name="Verify Private Engine",
                    path=".github/workflows/verify-private-engine.yml",
                    event=self.private_event,
                ),
                _run(
                    run_id=102,
                    name="Verify Production Stage Ladder",
                    path=".github/workflows/verify-production-stage-ladder.yml",
                    conclusion=self.stage_conclusion,
                ),
            ]
            if self.omit_workflow:
                runs = [item for item in runs if item["name"] != self.omit_workflow]
            return _Response({"total_count": len(runs), "workflow_runs": runs})
        raise urllib.error.URLError(f"unexpected URL {url}")


class ProductionCertificationGateTests(unittest.TestCase):
    def test_accepts_protected_current_main_with_exact_refs_and_successful_runs(self) -> None:
        opener = _Opener()
        result = verify_certified_production_source(
            repository=REPO,
            runner_sha=SHA,
            git_ref="refs/heads/main",
            token="token",
            opener=opener,
        )
        self.assertEqual(result["status"], "green")
        self.assertTrue(result["main_protected"])
        self.assertEqual(
            result["certification_refs"],
            [f"full-regression-green-{SHA}", f"stage-ladder-green-{SHA}"],
        )
        self.assertEqual(
            [item["name"] for item in result["certification_runs"]],
            ["Verify Private Engine", "Verify Production Stage Ladder"],
        )
        self.assertEqual([item["run_id"] for item in result["certification_runs"]], [101, 102])
        self.assertFalse(result["production_dispatch_performed"])
        actions_url = next(url for url in opener.urls if "/actions/runs?" in url)
        self.assertIn("head_sha=" + SHA, actions_url)
        self.assertIn("branch=main", actions_url)
        self.assertIn("event=push", actions_url)
        self.assertIn("status=success", actions_url)

    def test_rejects_unprotected_main(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "main is not protected"):
            verify_certified_production_source(
                repository=REPO,
                runner_sha=SHA,
                git_ref="refs/heads/main",
                token="token",
                opener=_Opener(protected=False),
            )

    def test_rejects_dispatch_not_bound_to_current_main(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "is not current main"):
            verify_certified_production_source(
                repository=REPO,
                runner_sha=SHA,
                git_ref="refs/heads/main",
                token="token",
                opener=_Opener(branch_sha="b" * 40),
            )

    def test_rejects_mismatched_certification_ref(self) -> None:
        bad_tag = f"full-regression-green-{SHA}"
        with self.assertRaisesRegex(RuntimeError, "does not bind exact Runner SHA"):
            verify_certified_production_source(
                repository=REPO,
                runner_sha=SHA,
                git_ref="refs/heads/main",
                token="token",
                opener=_Opener(bad_tag=bad_tag),
            )

    def test_rejects_tag_only_pr_certification(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "missing exact successful main-push certification run"):
            verify_certified_production_source(
                repository=REPO,
                runner_sha=SHA,
                git_ref="refs/heads/main",
                token="token",
                opener=_Opener(private_event="pull_request"),
            )

    def test_rejects_failed_stage_ladder_even_when_tags_match(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Verify Production Stage Ladder"):
            verify_certified_production_source(
                repository=REPO,
                runner_sha=SHA,
                git_ref="refs/heads/main",
                token="token",
                opener=_Opener(stage_conclusion="failure"),
            )

    def test_rejects_missing_canonical_run_even_when_tags_match(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Verify Private Engine"):
            verify_certified_production_source(
                repository=REPO,
                runner_sha=SHA,
                git_ref="refs/heads/main",
                token="token",
                opener=_Opener(omit_workflow="Verify Private Engine"),
            )

    def test_rejects_non_main_dispatch(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "main-only"):
            verify_certified_production_source(
                repository=REPO,
                runner_sha=SHA,
                git_ref="refs/heads/feature",
                token="token",
                opener=_Opener(),
            )

    def test_rejects_missing_token(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "GITHUB_TOKEN is required"):
            verify_certified_production_source(
                repository=REPO,
                runner_sha=SHA,
                git_ref="refs/heads/main",
                token="",
                opener=_Opener(),
            )


if __name__ == "__main__":
    unittest.main()
