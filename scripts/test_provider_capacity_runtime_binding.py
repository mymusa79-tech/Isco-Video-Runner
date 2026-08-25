from __future__ import annotations

import ast
import unittest
from pathlib import Path

from scripts import runtime_closure


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


class ProviderCapacityRuntimeBindingTests(unittest.TestCase):
    def test_capacity_is_installed_once_after_media_trust_before_core_contract(self) -> None:
        source = Path(runtime_closure.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        install = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "install_runtime_closure"
        )
        calls = [
            (node.lineno, _call_name(node))
            for node in ast.walk(install)
            if isinstance(node, ast.Call)
        ]
        media = [line for line, name in calls if name == "install_media_trust_boundary_v2"]
        capacity = [line for line, name in calls if name == "install_provider_capacity_v2"]
        core = [line for line, name in calls if name == "install_core_reliability_guard"]
        self.assertEqual(len(media), 1)
        self.assertEqual(len(capacity), 1)
        self.assertEqual(len(core), 1)
        self.assertLess(media[0], capacity[0])
        self.assertLess(capacity[0], core[0])


if __name__ == "__main__":
    unittest.main()
