from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from scripts import youtube_oauth_readonly_firewall as firewall


class YoutubeOAuthReadonlyFirewallTests(unittest.TestCase):
    def _response(self, status: int, payload: dict) -> Mock:
        response = Mock(spec=requests.Response)
        response.status_code = status
        response.ok = 200 <= status < 300
        response.json.return_value = payload
        return response

    def test_readonly_youtube_scopes_are_allowed(self) -> None:
        firewall.certify_readonly_scopes(
            {
                "https://www.googleapis.com/auth/youtube.readonly",
                "https://www.googleapis.com/auth/yt-analytics.readonly",
            }
        )

    def test_youtube_upload_scope_is_blocked(self) -> None:
        with self.assertRaisesRegex(
            firewall.YoutubeOAuthCapabilityError,
            "WRITE-CAPABLE_OR_UNKNOWN_YOUTUBE_OAUTH_SCOPE_BLOCKED",
        ):
            firewall.certify_readonly_scopes(
                {
                    "https://www.googleapis.com/auth/yt-analytics.readonly",
                    "https://www.googleapis.com/auth/youtube.upload",
                }
            )

    def test_broad_youtube_scope_is_blocked(self) -> None:
        with self.assertRaises(firewall.YoutubeOAuthCapabilityError):
            firewall.certify_readonly_scopes(
                {
                    "https://www.googleapis.com/auth/yt-analytics.readonly",
                    "https://www.googleapis.com/auth/youtube",
                }
            )

    def test_unknown_future_youtube_scope_is_fail_closed(self) -> None:
        with self.assertRaises(firewall.YoutubeOAuthCapabilityError):
            firewall.certify_readonly_scopes(
                {
                    "https://www.googleapis.com/auth/yt-analytics.readonly",
                    "https://www.googleapis.com/auth/youtube.some-future-capability",
                }
            )

    @patch("scripts.youtube_oauth_readonly_firewall.requests.get")
    @patch("scripts.youtube_oauth_readonly_firewall.requests.post")
    def test_tokeninfo_fallback_certifies_effective_scope(self, post: Mock, get: Mock) -> None:
        post.return_value = self._response(200, {"access_token": "redacted-test-token"})
        get.return_value = self._response(
            200,
            {
                "scope": (
                    "https://www.googleapis.com/auth/youtube.readonly "
                    "https://www.googleapis.com/auth/yt-analytics.readonly"
                )
            },
        )
        scopes = firewall.resolve_granted_scopes(
            client_id="client",
            client_secret="secret",
            refresh_token="refresh",
        )
        firewall.certify_readonly_scopes(scopes)
        get.assert_called_once()
        self.assertNotIn("redacted-test-token", str(get.call_args.kwargs.get("timeout")))

    def test_partial_runtime_oauth_materialization_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            secret_dir = Path(tmp) / "isco-secrets"
            secret_dir.mkdir()
            (secret_dir / "youtube-client-id").write_text("client", encoding="utf-8")
            with patch.dict(os.environ, {"RUNNER_TEMP": tmp}, clear=False):
                with self.assertRaisesRegex(
                    firewall.YoutubeOAuthCapabilityError,
                    "Partial YouTube OAuth materialization is forbidden",
                ):
                    firewall.enforce_from_runner_temp()

    def test_no_runtime_oauth_files_is_safe_no_capability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"RUNNER_TEMP": tmp}, clear=False):
                self.assertIsNone(firewall.enforce_from_runner_temp())

    def test_canonical_workflow_runs_firewall_before_engine_and_telegram_has_no_oauth(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        production = (repo_root / ".github/workflows/produce-resilient-v4.yml").read_text(encoding="utf-8")

        preflight_index = production.index("python -m scripts.provider_preflight")
        produce_index = production.index("- name: Produce with canonical V4 runtime")
        self.assertLess(preflight_index, produce_index)

        for marker in (
            "youtube-client-id",
            "youtube-client-secret",
            "youtube-refresh-token",
            "YOUTUBE_CLIENT_ID_FILE",
            "YOUTUBE_CLIENT_SECRET_FILE",
            "YOUTUBE_REFRESH_TOKEN_FILE",
        ):
            self.assertIn(marker, production)

        for workflow_name in ("telegram-production-request.yml", "telegram-editorial-control.yml"):
            telegram = (repo_root / f".github/workflows/{workflow_name}").read_text(encoding="utf-8")
            self.assertNotIn("YOUTUBE_CLIENT_ID", telegram)
            self.assertNotIn("YOUTUBE_CLIENT_SECRET", telegram)
            self.assertNotIn("YOUTUBE_REFRESH_TOKEN", telegram)

    def test_provider_preflight_entrypoints_resolve_package_in_clean_subprocess(self) -> None:
        """Exercise the real CLI boundary that import-only tests missed in Run 202."""
        repo_root = Path(__file__).resolve().parents[1]
        commands = (
            [sys.executable, "-m", "scripts.provider_preflight", "--help"],
            [sys.executable, "scripts/provider_preflight.py", "--help"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["RUNNER_TEMP"] = tmp
            env.pop("PYTHONPATH", None)
            for command in commands:
                with self.subTest(command=command):
                    completed = subprocess.run(
                        command,
                        cwd=repo_root,
                        env=env,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                        timeout=30,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertIn("--output", completed.stdout)


if __name__ == "__main__":
    unittest.main()
