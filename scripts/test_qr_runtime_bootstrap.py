from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from unittest.mock import patch

from scripts import qr_runtime_bootstrap as runtime


class QRRuntimeBootstrapTests(unittest.TestCase):
    def test_missing_runtime_outside_github_actions_never_installs(self) -> None:
        def which(name: str):
            if name in {"zbarimg", "ZXingReader"}:
                return None
            return f"/usr/bin/{name}"

        with patch.dict(os.environ, {}, clear=True), patch.object(
            runtime.shutil, "which", side_effect=which
        ), patch.object(runtime.subprocess, "run") as run:
            with self.assertRaisesRegex(runtime.QRRuntimeBootstrapError, "runtime_unavailable"):
                runtime.ensure_qr_confirmation_runtime()
        run.assert_not_called()

    def test_privileged_install_receives_scrubbed_environment_and_exact_versions(self) -> None:
        calls = {"installed": False}

        def which(name: str):
            if name in {"zbarimg", "ZXingReader"} and not calls["installed"]:
                return None
            mapping = {
                "zbarimg": "/usr/bin/zbarimg",
                "ZXingReader": "/usr/bin/ZXingReader",
                "sudo": "/usr/bin/sudo",
                "apt-get": "/usr/bin/apt-get",
                "dpkg-query": "/usr/bin/dpkg-query",
            }
            return mapping.get(name, f"/usr/bin/{name}")

        def run_side_effect(argv, **kwargs):
            is_apt = any(str(item) == "apt-get" or str(item).endswith("/apt-get") for item in argv)
            if is_apt:
                calls["installed"] = True
                env = kwargs["env"]
                self.assertNotIn("GEMINI_API_KEY", env)
                self.assertNotIn("GROQ_API_KEY_FILE", env)
                self.assertNotIn("TELEGRAM_BOT_TOKEN_FILE", env)
                self.assertIn(f"{runtime.ZBAR_PACKAGE}={runtime.ZBAR_VERSION}", argv)
                self.assertIn(f"{runtime.ZXING_PACKAGE}={runtime.ZXING_VERSION}", argv)
                self.assertIn("--no-install-recommends", argv)
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            package = argv[-1]
            version = {
                runtime.ZBAR_PACKAGE: runtime.ZBAR_VERSION,
                runtime.ZXING_PACKAGE: runtime.ZXING_VERSION,
            }[package]
            return subprocess.CompletedProcess(argv, 0, stdout=version, stderr="")

        env = {
            "GITHUB_ACTIONS": "true",
            "PATH": "/usr/bin:/bin",
            "GEMINI_API_KEY": "must-not-leak",
            "GROQ_API_KEY_FILE": "/tmp/secret",
            "TELEGRAM_BOT_TOKEN_FILE": "/tmp/telegram",
        }
        with patch.dict(os.environ, env, clear=True), patch.object(
            runtime.shutil, "which", side_effect=which
        ), patch.object(runtime.subprocess, "run", side_effect=run_side_effect):
            tools = runtime.ensure_qr_confirmation_runtime()
        self.assertEqual(tools.zbarimg, "/usr/bin/zbarimg")
        self.assertEqual(tools.zxing_reader, "/usr/bin/ZXingReader")

    def test_existing_tools_with_wrong_package_version_fail_closed(self) -> None:
        def which(name: str):
            mapping = {
                "zbarimg": "/usr/bin/zbarimg",
                "ZXingReader": "/usr/bin/ZXingReader",
                "dpkg-query": "/usr/bin/dpkg-query",
            }
            return mapping.get(name, f"/usr/bin/{name}")

        with patch.object(runtime.shutil, "which", side_effect=which), patch.object(
            runtime.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, stdout="unexpected-version", stderr=""),
        ):
            with self.assertRaisesRegex(runtime.QRRuntimeBootstrapError, "version_mismatch"):
                runtime.ensure_qr_confirmation_runtime(allow_install=False)

    def test_zz_real_github_actions_runtime_is_pinned_and_executable(self) -> None:
        if str(os.environ.get("GITHUB_ACTIONS") or "").lower() != "true":
            self.skipTest("real apt-backed QR runtime smoke is GitHub Actions only")

        tools = runtime.ensure_qr_confirmation_runtime(allow_install=True)
        runtime._verify_pinned_versions()
        self.assertTrue(os.path.isabs(tools.zbarimg))
        self.assertTrue(os.path.isabs(tools.zxing_reader))
        self.assertTrue(shutil.which("ZXingWriter"), "zxing-cpp-tools must include ZXingWriter")

        zbar_version = subprocess.run(
            [tools.zbarimg, "--version"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        self.assertEqual(zbar_version.returncode, 0, zbar_version.stderr)

        zxing_version = subprocess.run(
            [tools.zxing_reader, "-version"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        self.assertEqual(zxing_version.returncode, 0, zxing_version.stderr)


if __name__ == "__main__":
    unittest.main()
