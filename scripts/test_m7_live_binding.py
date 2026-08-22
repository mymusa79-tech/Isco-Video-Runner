from __future__ import annotations

import os
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import scripts.m7_live_binding as bridge


class M7RunnerInstallerTests(unittest.TestCase):
    def tearDown(self) -> None:
        current = bridge.orchestrator.produce
        original = getattr(current, "_isco_m7_original", None)
        if original is not None:
            bridge.orchestrator.produce = original

    def test_installer_is_idempotent_captures_keys_and_chains_security(self) -> None:
        calls = []

        def core(*args, **kwargs):
            calls.append(("core", args, kwargs))
            return "out"

        @contextmanager
        def scope(module, *, pexels_api_key, pixabay_api_key):
            calls.append(("scope", pexels_api_key, pixabay_api_key, module is bridge.orchestrator))
            yield

        with patch.object(bridge.orchestrator, "produce", core), patch.object(
            bridge, "live_m7_binding_scope", scope
        ), patch.object(
            bridge, "install_security_v1_live_binding"
        ) as security, patch.dict(
            os.environ,
            {"PEXELS_API_KEY": "pexels-secret", "PIXABAY_API_KEY": "pixabay-secret"},
            clear=False,
        ):
            bridge.install_m7_live_binding()
            wrapped = bridge.orchestrator.produce
            bridge.install_m7_live_binding()
            self.assertIs(bridge.orchestrator.produce, wrapped)
            result = wrapped(topic="x")

        self.assertEqual(result, "out")
        self.assertEqual(calls[0], ("scope", "pexels-secret", "pixabay-secret", True))
        self.assertEqual(calls[1][0], "core")
        self.assertEqual(security.call_count, 2)

    def test_missing_pexels_preserves_core_authoritative_failure_path(self) -> None:
        calls = []

        def core(*args, **kwargs):
            calls.append("core")
            raise RuntimeError("missing pexels from core")

        with patch.object(bridge.orchestrator, "produce", core), patch.object(
            bridge, "install_security_v1_live_binding"
        ), patch.dict(
            os.environ, {"PEXELS_API_KEY": "", "PIXABAY_API_KEY": ""}, clear=False
        ):
            bridge.install_m7_live_binding()
            with self.assertRaisesRegex(RuntimeError, "missing pexels from core"):
                bridge.orchestrator.produce()
        self.assertEqual(calls, ["core"])


if __name__ == "__main__":
    unittest.main()
