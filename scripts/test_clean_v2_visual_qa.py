from __future__ import annotations

import unittest
from pathlib import Path

from clean_v2.visual_qa import LAYER_ID, _lookup


class FinalCutVisualQAContractTests(unittest.TestCase):
    SOURCE = Path("clean_v2/visual_qa.py")

    def test_layer_identity_is_stable(self) -> None:
        self.assertEqual(LAYER_ID, "final_cut_visual_qa_v1")

    def test_adapter_reuses_existing_visual_qa_owners(self) -> None:
        text = self.SOURCE.read_text(encoding="utf-8")
        required = (
            "audit_video_preview",
            "make_review_preview",
            "FINAL_CUT_TARGET_SEMANTIC_FLOOR",
            "is_final_cut_ready",
            "semantic_floor",
            "install_vision_provider_reliability",
            "install_run181_vision_mesh_closure",
            "vision_provider_circuit_scope",
        )
        for needle in required:
            self.assertIn(needle, text)

    def test_layer_is_auditor_only_not_selector_or_repair_path(self) -> None:
        text = self.SOURCE.read_text(encoding="utf-8")
        forbidden = (
            "select_with_recovery",
            "suggest_alternate_visual_query",
            "alternate_search",
            "pexels_search",
            "pixabay_provider.search",
            "render_video(",
        )
        for needle in forbidden:
            self.assertNotIn(needle, text)

    def test_layer_does_not_activate_later_quality_or_secondary_deliverables(self) -> None:
        text = self.SOURCE.read_text(encoding="utf-8").casefold()
        forbidden = (
            "run_gold_enforce_phase4",
            "enforce_viewer_quality_contract",
            "build_budgeted_thumbnail_package",
            "sibling_short",
            "shorts_production_binding",
            "text_audit",
        )
        for needle in forbidden:
            self.assertNotIn(needle, text)

    def test_lookup_preserves_only_real_section_ids(self) -> None:
        payload = {
            "sections": [
                {"id": "s1", "value": 1},
                {"id": "", "value": 2},
                {"value": 3},
                {"id": "s2", "value": 4},
            ]
        }
        self.assertEqual(
            _lookup(payload, "sections"),
            {
                "s1": {"id": "s1", "value": 1},
                "s2": {"id": "s2", "value": 4},
            },
        )


if __name__ == "__main__":
    unittest.main()
