from __future__ import annotations

import ast
import unittest
from pathlib import Path


RUNTIME = Path(__file__).with_name("runtime_closure.py")
RUNNER = Path(__file__).with_name("run_v3_voice.py")


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


class AudioMasteringRuntimeContractTests(unittest.TestCase):
    def test_runtime_closure_installs_audio_binding(self) -> None:
        tree = ast.parse(RUNTIME.read_text(encoding="utf-8"))
        fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "install_runtime_closure")
        calls = [_call_name(node) for node in ast.walk(fn) if isinstance(node, ast.Call)]
        self.assertIn("install_audio_mastering_live_binding", calls)

    def test_runtime_closure_and_outer_cinematic_seam_are_installed_before_produce(self) -> None:
        tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
        fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        calls = [
            (node.lineno, _call_name(node))
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
        ]
        closure = [line for line, name in calls if name == "install_runtime_closure"]
        cinematic = [line for line, name in calls if name == "install_cinematic_runtime_port"]
        produce = [line for line, name in calls if name == "produce"]
        self.assertEqual(len(closure), 1)
        self.assertEqual(len(cinematic), 1)
        self.assertEqual(len(produce), 1)
        self.assertLess(closure[0], cinematic[0])
        self.assertLess(cinematic[0], produce[0])


if __name__ == "__main__":
    unittest.main()
