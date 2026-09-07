from __future__ import annotations

import inspect
import unittest
from unittest import mock

from scripts import orchestration_media_port
from scripts import run183_visual_retrieval_closure as run183
from scripts import run221_anchor_preserving_visual_retrieval as run221


RUN221_WARDROBE_QUERY = (
    "A contemplative person standing quietly in front of a minimalist wardrobe in soft "
    "morning sunlight, cinematic documentary style, realistic lighting"
)
STYLE_ONLY_BAD_RECOVERY = "cinematic contemplative documentary front"


class Run221AnchorPreservingVisualRetrievalTests(unittest.TestCase):
    def test_exact_run221_scene_extracts_wardrobe_as_concrete_anchor(self) -> None:
        subject, anchors, context = run221.anchor_contract(RUN221_WARDROBE_QUERY)
        self.assertEqual(subject, ("back",))
        self.assertEqual(anchors, ("wardrobe",))
        self.assertEqual(context, ("morning", "sunlight"))

    def test_exact_run221_family_never_collapses_to_style_only_recovery(self) -> None:
        base = run183.SemanticRetrievalFamily(
            primary=RUN221_WARDROBE_QUERY,
            alternates=(STYLE_ONLY_BAD_RECOVERY,),
            labels=frozenset({"calm"}),
        )
        family = run221.rewrite_semantic_family(base, RUN221_WARDROBE_QUERY)

        self.assertIn("wardrobe", family.primary.split())
        self.assertIn("back", family.primary.split())
        self.assertNotIn("cinematic", family.primary.split())
        self.assertNotIn("documentary", family.primary.split())
        self.assertNotIn(STYLE_ONLY_BAD_RECOVERY, family.alternates)
        self.assertEqual(len(family.alternates), run183.MAX_ALTERNATE_QUERY_FANOUT)
        for query in family.alternates:
            self.assertTrue(run221.preserves_anchor_contract(query, RUN221_WARDROBE_QUERY), query)
            self.assertTrue(set(query.split()) & {"wardrobe", "clothes", "closet", "outfit"})
            self.assertTrue(set(query.split()) & {"back", "hands", "silhouette", "shadow", "faceless", "anonymous"})

    def test_controlled_relaxation_drops_context_before_wardrobe_anchor(self) -> None:
        primary, alternates = run221.anchor_query_family(RUN221_WARDROBE_QUERY)
        self.assertIn("morning", primary.split())
        self.assertIn("sunlight", primary.split())
        self.assertEqual(len(alternates), 2)
        self.assertIn("morning", alternates[0].split())
        self.assertNotIn("sunlight", alternates[0].split())
        self.assertNotIn("morning", alternates[1].split())
        self.assertNotIn("sunlight", alternates[1].split())
        for query in (primary, *alternates):
            self.assertTrue(run221.preserves_anchor_contract(query, RUN221_WARDROBE_QUERY), query)

    def test_literal_anchor_is_kept_while_one_stock_synonym_expands_it(self) -> None:
        primary, alternates = run221.anchor_query_family(RUN221_WARDROBE_QUERY)
        self.assertIn("wardrobe", primary.split())
        self.assertIn("clothes", primary.split())
        self.assertIn("wardrobe", alternates[0].split())
        self.assertIn("closet", alternates[0].split())

    def test_existing_safe_human_framing_is_preserved(self) -> None:
        query = "hands selecting clothes wardrobe morning"
        subject, anchors, context = run221.anchor_contract(query)
        self.assertEqual(subject, ("hands",))
        self.assertTrue(anchors)
        primary, alternates = run221.anchor_query_family(query)
        self.assertIn("hands", primary.split())
        self.assertTrue(run221.preserves_anchor_contract(primary, query))
        self.assertTrue(all(run221.preserves_anchor_contract(item, query) for item in alternates))

    def test_run214_decision_scene_remains_owned_by_run214(self) -> None:
        query = (
            "Cinematic medium shot of a person choosing between coffee and a smartphone "
            "at a kitchen counter in the morning"
        )
        self.assertEqual(run221.anchor_contract(query), ((), (), ()))
        self.assertEqual(run221.anchor_query_family(query), ("", ()))

    def test_nonhuman_scene_keeps_existing_retrieval_owner(self) -> None:
        query = "notebook on desk in warm morning sunlight"
        self.assertEqual(run221.anchor_contract(query), ((), (), ()))
        self.assertEqual(run221.anchor_query_family(query), ("", ()))

    def test_unknown_human_scene_falls_back_instead_of_inventing_anchor(self) -> None:
        query = "person beside sculpture in museum morning light"
        self.assertEqual(run221.anchor_contract(query), ((), (), ()))
        self.assertEqual(run221.anchor_query_family(query), ("", ()))

    def test_stock_query_wrapper_is_production_scoped(self) -> None:
        wrapped = run221._wrap_stock_query(lambda query: "legacy-query")
        with mock.patch.object(run221, "runtime_active", return_value=False):
            self.assertEqual(wrapped(RUN221_WARDROBE_QUERY), "legacy-query")
        with mock.patch.object(run221, "runtime_active", return_value=True):
            active = wrapped(RUN221_WARDROBE_QUERY)
        self.assertNotEqual(active, "legacy-query")
        self.assertTrue(run221.preserves_anchor_contract(active, RUN221_WARDROBE_QUERY))

    def test_semantic_family_wrapper_preserves_existing_behavior_outside_runtime(self) -> None:
        base = run183.SemanticRetrievalFamily(
            primary="legacy-primary",
            alternates=(STYLE_ONLY_BAD_RECOVERY,),
            labels=frozenset({"legacy"}),
        )
        wrapped = run221._wrap_semantic_family(lambda _visual, _narration="": base)
        with mock.patch.object(run221, "runtime_active", return_value=False):
            self.assertEqual(wrapped(RUN221_WARDROBE_QUERY), base)
        with mock.patch.object(run221, "runtime_active", return_value=True):
            active = wrapped(RUN221_WARDROBE_QUERY)
        self.assertNotEqual(active, base)
        self.assertIn("run221_anchor_preserved", active.labels)

    def test_query_count_and_length_remain_existing_bounded_shape(self) -> None:
        primary, alternates = run221.anchor_query_family(RUN221_WARDROBE_QUERY)
        self.assertLessEqual(len(primary.split()), run221.MAX_QUERY_TOKENS)
        self.assertLessEqual(len(alternates), run183.MAX_ALTERNATE_QUERY_FANOUT)
        self.assertTrue(all(len(query.split()) <= run221.MAX_QUERY_TOKENS for query in alternates))

    def test_media_port_composes_run221_after_run212(self) -> None:
        source = inspect.getsource(orchestration_media_port.install_media_runtime_port)
        run212_pos = source.index("install_shared_visual_candidate_utilization")
        run221_pos = source.index("install_run221_anchor_preserving_visual_retrieval")
        self.assertLess(run212_pos, run221_pos)


if __name__ == "__main__":
    unittest.main()
