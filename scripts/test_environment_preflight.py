from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import requests

from scripts import environment_preflight as envp


class EnvironmentPreflightTests(unittest.TestCase):
    def _response(self, status: int) -> Mock:
        response = Mock(spec=requests.Response)
        response.status_code = status
        response.ok = 200 <= status < 300
        return response

    def test_absent_release_tag_is_available(self) -> None:
        with patch.object(envp.requests, "get", return_value=self._response(404)):
            self.assertEqual(envp._release_namespace_status("owner/repo", "video-1"), "available")

    def test_existing_release_tag_fails_closed(self) -> None:
        with patch.object(envp.requests, "get", return_value=self._response(200)):
            with self.assertRaisesRegex(RuntimeError, "existing release tag blocks"):
                envp._release_namespace_status("owner/repo", "video-1")

    def test_uncertain_namespace_is_never_treated_as_absent(self) -> None:
        for status in (403, 429, 500, 503):
            with self.subTest(status=status):
                with patch.object(envp.requests, "get", return_value=self._response(status)):
                    with self.assertRaises(RuntimeError):
                        envp._release_namespace_status("owner/repo", "video-1")

    def test_required_binary_missing_is_blocking(self) -> None:
        with patch.object(envp.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "required runtime binary missing"):
                envp._binary("ffmpeg")

    def test_required_package_missing_is_blocking(self) -> None:
        with patch.object(envp.md, "version", side_effect=envp.md.PackageNotFoundError):
            with self.assertRaisesRegex(RuntimeError, "required runtime package missing"):
                envp._version("piper-tts")


if __name__ == "__main__":
    unittest.main()
