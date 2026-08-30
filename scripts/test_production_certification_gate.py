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


class _Opener:
    def __init__(self, *, protected: bool = True, branch_sha: str = SHA, bad_tag: str | None = None):
        self.protected = protected
        self.branch_sha = branch_sha
        self.bad_tag = bad_tag
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
        raise urllib.error.URLError(f"unexpected URL {url}")


class ProductionCertificationGateTests(unittest.TestCase):
    def test_accepts_protected_current_main_with_both_exact_sha_refs(self) -> None:
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
        self.assertFalse(result["production_dispatch_performed"])

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
