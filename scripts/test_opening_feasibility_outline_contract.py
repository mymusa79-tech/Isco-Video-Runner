from __future__ import annotations

import unittest
from types import SimpleNamespace

from scripts import opening_feasibility_guard as guard
from scripts import planning_stage_contract as planning_contract


class OpeningFeasibilityCanonicalOutlineRegressionTest(unittest.TestCase):
    def test_search_derivative_never_enters_canonical_section_brief(self) -> None:
        outline = {
            "section_briefs": [
                {
                    "visual_query": "person sitting at desk with notebook",
                }
            ]
        }

        result = guard._preserve_outline_visual_intent(outline, fmt="film")

        self.assertIs(result, outline)
        self.assertEqual(
            result["section_briefs"][0]["visual_query"],
            "person sitting at desk with notebook",
        )
        self.assertNotIn("stock_search_query", result["section_briefs"][0])
        # The retrieval transform still exists at the retrieval boundary; removing the
        # canonical field must not remove stock-search capability.
        self.assertTrue(guard.stock_safe_search_query("person sitting at desk with notebook"))

    def test_existing_structural_drift_is_not_silently_cleaned(self) -> None:
        brief = {
            "visual_query": "quiet room natural light",
            "stock_search_query": "quiet room notebook",
        }
        outline = {"section_briefs": [brief]}

        result = guard._preserve_outline_visual_intent(outline, fmt="film")

        # The guard is not a schema normalizer. Upstream/provider structural drift must
        # remain visible so the canonical contract rejects it fail-closed.
        self.assertIn("stock_search_query", result["section_briefs"][0])

        strict_brief_schema = {
            "type": "object",
            "properties": {"visual_query": {"type": "string"}},
            "required": ["visual_query"],
            "additionalProperties": False,
        }
        with self.assertRaises(planning_contract.PlanningStageError):
            planning_contract._validate_schema(
                result["section_briefs"][0],
                strict_brief_schema,
                SimpleNamespace(stage_id="planning.editorial_outline_sections"),
            )


if __name__ == "__main__":
    unittest.main()
