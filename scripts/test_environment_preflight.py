from __future__ import annotations

import os
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
        with patch.object(envp.requests, "get", side_effect=[self._response(404), self._response(404)]):
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

    def test_orphan_git_tag_fails_closed(self) -> None:
        with patch.object(
            envp.requests,
            "get",
            side_effect=[self._response(404), self._response(200)],
        ):
            with self.assertRaisesRegex(RuntimeError, "orphan Git tag blocks"):
                envp._release_namespace_status("owner/repo", "video-1")

    def test_uncertain_git_tag_probe_is_never_treated_as_absent(self) -> None:
        for status in (403, 429, 500, 503):
            with self.subTest(status=status):
                with patch.object(
                    envp.requests,
                    "get",
                    side_effect=[self._response(404), self._response(status)],
                ):
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

    def test_local_probe_environment_strips_credentials(self) -> None:
        with patch.dict(os.environ, {"SAFE": "yes", "GITHUB_TOKEN": "secret", "PEXELS_API_KEY": "secret2"}, clear=True):
            cleaned = envp._secret_free_env()
        self.assertEqual(cleaned, {"SAFE": "yes"})

    def test_ffmpeg_missing_encoder_is_blocking(self) -> None:
        with patch.object(envp, "_require_command", side_effect=[" V..... other_encoder\n", "blackdetect silencedetect freezedetect loudnorm subtitles"]):
            with self.assertRaisesRegex(RuntimeError, "libx264"):
                envp._certify_ffmpeg()

    def test_ffmpeg_missing_filter_is_blocking(self) -> None:
        with patch.object(envp, "_require_command", side_effect=[" V..... libx264\n", "blackdetect silencedetect loudnorm subtitles"]):
            with self.assertRaisesRegex(RuntimeError, "freezedetect"):
                envp._certify_ffmpeg()

    def test_tesseract_requires_arabic_language_data(self) -> None:
        with patch.object(envp, "_require_command", return_value="List of available languages (2):\neng\nosd\n"):
            with self.assertRaisesRegex(RuntimeError, "Arabic"):
                envp._certify_tesseract_arabic()

    def test_release_namespace_auth_header_is_used_without_leaking_elsewhere(self) -> None:
        with patch.object(
            envp.requests,
            "get",
            side_effect=[self._response(404), self._response(404)],
        ) as get:
            self.assertEqual(envp._release_namespace_status("owner/repo", "video-2", token="gh-secret"), "available")
        self.assertEqual(get.call_count, 2)
        for call in get.call_args_list:
            self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer gh-secret")


if __name__ == "__main__":
    unittest.main()
