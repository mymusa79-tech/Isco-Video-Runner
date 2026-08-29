from __future__ import annotations

import ast
import unittest
from pathlib import Path

from scripts import planning_stage_contract as contract


class PlanningAppendStageContractTests(unittest.TestCase):
    def _bound(self, spec: contract.PlanningStageSpec) -> contract.PlanningStageContract:
        return contract.bind_request_contract(spec, "effective append input")

    def test_candidate_contract_allows_only_ordered_subset_and_is_never_cached(self) -> None:
        spec = contract.append_stage_spec(
            ["s1", "s2", "s3"],
            allow_ordered_subset=True,
        )
        self.assertEqual(spec.contract_id, "planning.append_only_repair.candidate.v1")
        self.assertFalse(spec.cache_policy.read)
        self.assertFalse(spec.cache_policy.write)
        payload = {
            "additions": [
                {"id": "s1", "append_text": "زيادة أولى"},
                {"id": "s3", "append_text": "زيادة ثالثة"},
            ]
        }
        self.assertEqual(contract.validate_response(self._bound(spec), payload), payload)

    def test_candidate_contract_rejects_reordered_or_unknown_ids(self) -> None:
        bound = self._bound(
            contract.append_stage_spec(
                ["s1", "s2", "s3"],
                allow_ordered_subset=True,
            )
        )
        bad_payloads = (
            {
                "additions": [
                    {"id": "s3", "append_text": "ثالث"},
                    {"id": "s1", "append_text": "أول"},
                ]
            },
            {"additions": [{"id": "UNKNOWN", "append_text": "نص"}]},
        )
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(contract.PlanningStageError) as captured:
                    contract.validate_response(bound, payload)
                self.assertEqual(
                    captured.exception.code,
                    contract.PlanningErrorCode.SEMANTIC_INVALID,
                )

    def test_exact_append_contract_rejects_partial_response_and_is_never_cached(self) -> None:
        spec = contract.append_stage_spec(["s1", "s2"])
        self.assertEqual(spec.contract_id, "planning.append_only_repair.exact.v1")
        self.assertFalse(spec.cache_policy.read)
        self.assertFalse(spec.cache_policy.write)
        with self.assertRaises(contract.PlanningStageError) as captured:
            contract.validate_response(
                self._bound(spec),
                {"additions": [{"id": "s1", "append_text": "جزء"}]},
            )
        self.assertEqual(
            captured.exception.code,
            contract.PlanningErrorCode.STRUCTURAL_INVALID,
        )

    def test_every_direct_append_provider_request_has_an_explicit_python_scope(self) -> None:
        source_path = Path(__file__).with_name("append_retry_guard.py")
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        target = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_repair_all_residual_underlength"
        )

        calls: list[tuple[int, bool]] = []

        class ScopeVisitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.scope_depth = 0

            def visit_With(self, node: ast.With) -> None:
                scoped = any(
                    "stage_contract.request_stage_scope" in ast.unparse(item.context_expr)
                    for item in node.items
                )
                if scoped:
                    self.scope_depth += 1
                for statement in node.body:
                    self.visit(statement)
                if scoped:
                    self.scope_depth -= 1

            def visit_Call(self, node: ast.Call) -> None:
                if ast.unparse(node.func) == "staged.json_text":
                    calls.append((node.lineno, self.scope_depth > 0))
                self.generic_visit(node)

        ScopeVisitor().visit(target)
        self.assertEqual(len(calls), 3, calls)
        self.assertTrue(all(scoped for _line, scoped in calls), calls)

        source = source_path.read_text(encoding="utf-8")
        self.assertIn(
            "allow_ordered_subset=current_words < minimum",
            source,
        )
        self.assertIn("append_stage_spec(pending_ids)", source)
        self.assertIn("append_stage_spec(rescue_ids)", source)


if __name__ == "__main__":
    unittest.main()
