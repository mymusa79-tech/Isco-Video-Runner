from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ROUTER_PATH = ROOT / "task_level_planner_router.py"


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing function: {name}")


def _calls(fn: ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


class LegacyPlanningAuthorityRemovalTests(unittest.TestCase):
    """Permanent authority invariant, not an implementation-topology snapshot.

    task_level_planner_router remains a live compatibility/provider-helper module, but
    it must never again infer a schema from prompt wording or own durable Planning cache
    reads/writes. Those two authorities belong only to planning_stage_contract.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = ROUTER_PATH.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_legacy_schema_resolver_has_no_prompt_inference_authority(self) -> None:
        fn = _function(self.tree, "_structured_schema_for_prompt")
        text = ast.get_source_segment(self.source, fn) or ""
        self.assertIn("LEGACY_PROMPT_SCHEMA_AUTHORITY_REMOVED", text)
        for historical_selector in (
            "section_briefs",
            "with EXACTLY",
            "Return ONLY JSON",
            "editorial_outline",
            "append_only_repair",
        ):
            self.assertNotIn(historical_selector, text)

    def test_legacy_checkpoint_symbols_are_fail_closed_not_io_owners(self) -> None:
        for name in ("_load_checkpoint", "_save_checkpoint"):
            fn = _function(self.tree, name)
            calls = _calls(fn)
            self.assertNotIn("read_text", calls)
            self.assertNotIn("write_text", calls)
            self.assertNotIn("replace", calls)
            text = ast.get_source_segment(self.source, fn) or ""
            self.assertIn("LEGACY_PLANNING_CACHE_AUTHORITY_REMOVED", text)

    def test_install_router_never_reads_or_writes_durable_checkpoint(self) -> None:
        fn = _function(self.tree, "install_router")
        calls = _calls(fn)
        self.assertNotIn("_load_checkpoint", calls)
        self.assertNotIn("_save_checkpoint", calls)
        text = ast.get_source_segment(self.source, fn) or ""
        self.assertNotIn("CACHE_PATH", text)
        self.assertNotIn("Planning checkpoint hit", text)

    def test_live_provider_helpers_are_preserved(self) -> None:
        names = {node.name for node in self.tree.body if isinstance(node, ast.FunctionDef)}
        for helper in (
            "_groq_call",
            "_mistral_call",
            "_openrouter_call_with_repair",
            "_record_attempt",
            "write_planning_telemetry",
            "install_router",
        ):
            self.assertIn(helper, names)


if __name__ == "__main__":
    unittest.main()
