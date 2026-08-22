from __future__ import annotations

import ast
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import runtime_closure


RUNNER = Path(__file__).with_name("run_v3_voice.py")


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


class RuntimeClosureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = RUNNER.read_text(encoding="utf-8")
        tree = ast.parse(cls.text)
        cls.main = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"
        )

    def test_run71_installer_is_bound_after_append_guard(self) -> None:
        calls = [
            (node.lineno, _call_name(node))
            for node in ast.walk(self.main)
            if isinstance(node, ast.Call)
        ]
        append = [line for line, name in calls if name == "install_append_retry_guard"]
        closure = [line for line, name in calls if name == "install_runtime_closure"]
        produce = [line for line, name in calls if name == "produce"]
        self.assertEqual(len(append), 1)
        self.assertEqual(len(closure), 1)
        self.assertEqual(len(produce), 1)
        self.assertLess(append[0], closure[0])
        self.assertLess(closure[0], produce[0])

    def test_g1_g2_runs_once_after_gold_before_manifest(self) -> None:
        calls = [
            (node.lineno, _call_name(node))
            for node in ast.walk(self.main)
            if isinstance(node, ast.Call)
        ]
        gold = [line for line, name in calls if name == "run_gold_enforce_phase4"]
        observer = [line for line, name in calls if name == "run_post_gold_observers"]
        manifest = [line for line, name in calls if name == "_write_production_manifest"]
        self.assertEqual(len(gold), 1)
        self.assertEqual(len(observer), 1)
        self.assertEqual(len(manifest), 1)
        self.assertLess(gold[0], observer[0])
        self.assertLess(observer[0], manifest[0])

    def test_post_gold_observer_uses_optional_env_key_and_never_raises(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch.object(
            runtime_closure,
            "run_groq_audio_audit",
            side_effect=RuntimeError("synthetic"),
        ) as audit:
            result = runtime_closure.run_post_gold_observers(Path("output/example"))
        audit.assert_called_once_with(Path("output/example"), api_key="")
        self.assertEqual(result["mode"], "observe_only")
        self.assertEqual(result["decision"], "audit_error")

    def test_post_gold_observer_reads_existing_secret_file_without_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            key_path = Path(temp_dir) / "groq"
            key_path.write_text("secret-token", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"GROQ_API_KEY_FILE": str(key_path)},
                clear=True,
            ), patch.object(
                runtime_closure,
                "run_groq_audio_audit",
                return_value={"decision": "pass"},
            ) as audit:
                result = runtime_closure.run_post_gold_observers(Path("output/example"))
            audit.assert_called_once_with(Path("output/example"), api_key="secret-token")
            self.assertEqual(result["decision"], "pass")
            self.assertTrue(key_path.exists())

    def test_runtime_closure_installs_run71_recovery(self) -> None:
        with patch.object(runtime_closure, "install_attempt10_append_bound_recovery") as install:
            runtime_closure.install_runtime_closure()
        install.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
