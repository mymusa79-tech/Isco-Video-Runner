from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("cloudflare_gold_preflight.py")
PROVIDER_PREFLIGHT_PATH = Path(__file__).with_name("provider_preflight.py")


def _load_preflight_with_stubbed_fallback():
    fallback = types.ModuleType("scripts.gold_cloudflare_vision_fallback")

    class CloudflareGoldVisionUnavailable(RuntimeError):
        pass

    fallback.CloudflareGoldVisionUnavailable = CloudflareGoldVisionUnavailable
    fallback._credentials = lambda: ("token", "0" * 32)
    fallback._enabled = lambda: True
    fallback._prove_model_access = lambda *_args, **_kwargs: None
    fallback._prove_workers_free = lambda *_args, **_kwargs: None

    module_name = "_cloudflare_gold_preflight_optional_contract_under_test"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load Cloudflare Gold preflight module")
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        sys.modules,
        {
            "scripts.gold_cloudflare_vision_fallback": fallback,
            module_name: module,
        },
    ):
        spec.loader.exec_module(module)
    return module, CloudflareGoldVisionUnavailable


class OptionalCloudflareGoldPreflightContractTests(unittest.TestCase):
    def test_optional_cloudflare_unavailable_disables_only_that_route(self) -> None:
        module, unavailable = _load_preflight_with_stubbed_fallback()
        with tempfile.TemporaryDirectory() as tmp:
            github_env = Path(tmp) / "github-env"
            environment = {
                "GITHUB_ENV": str(github_env),
                "CLOUDFLARE_GOLD_VISION_FREE_ONLY": "true",
            }

            def fail_strict_probe() -> None:
                raise unavailable("Billing Read missing")

            output = io.StringIO()
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
                module,
                "preflight_gold_cloudflare_from_runner_temp",
                fail_strict_probe,
            ), contextlib.redirect_stdout(output):
                self.assertFalse(module.preflight_optional_gold_cloudflare_from_runner_temp())
                self.assertEqual(os.environ["CLOUDFLARE_GOLD_VISION_FREE_ONLY"], "false")
                self.assertEqual(
                    github_env.read_text(encoding="utf-8"),
                    "CLOUDFLARE_GOLD_VISION_FREE_ONLY=false\n",
                )

            text = output.getvalue()
            self.assertIn("OPTIONAL_UNAVAILABLE", text)
            self.assertIn("production continues without Cloudflare Gold fallback", text)

    def test_optional_cloudflare_success_remains_enabled(self) -> None:
        module, _ = _load_preflight_with_stubbed_fallback()
        calls: list[str] = []
        environment = {"CLOUDFLARE_GOLD_VISION_FREE_ONLY": "true"}
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
            module,
            "preflight_gold_cloudflare_from_runner_temp",
            lambda: calls.append("strict-probe"),
        ):
            self.assertTrue(module.preflight_optional_gold_cloudflare_from_runner_temp())
            self.assertEqual(calls, ["strict-probe"])
            self.assertEqual(os.environ["CLOUDFLARE_GOLD_VISION_FREE_ONLY"], "true")

    def test_optional_cloudflare_does_not_swallow_programming_errors(self) -> None:
        module, _ = _load_preflight_with_stubbed_fallback()

        def programming_bug() -> None:
            raise RuntimeError("unexpected bug")

        with mock.patch.dict(
            os.environ,
            {"CLOUDFLARE_GOLD_VISION_FREE_ONLY": "true"},
            clear=False,
        ), mock.patch.object(
            module,
            "preflight_gold_cloudflare_from_runner_temp",
            programming_bug,
        ):
            with self.assertRaisesRegex(RuntimeError, "unexpected bug"):
                module.preflight_optional_gold_cloudflare_from_runner_temp()

    def test_provider_preflight_wires_optional_probe_not_strict_probe(self) -> None:
        tree = ast.parse(PROVIDER_PREFLIGHT_PATH.read_text(encoding="utf-8"))
        called_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("preflight_optional_gold_cloudflare_from_runner_temp", called_names)
        self.assertNotIn("preflight_gold_cloudflare_from_runner_temp", called_names)


if __name__ == "__main__":
    unittest.main()
