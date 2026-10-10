from __future__ import annotations

import unittest

from clean_v2.media import _diversify_recovery_candidates
from clean_v2.visual_qa import (
    AlternateQueryError,
    _validate_alternate_query,
)


RUN116_AUTHORED_ALTERNATE = (
    "hands dropping a pen in frustration while looking at a messy notebook "
    "filled with crossed-out notes"
)


class Run116VisualRecoverySearchTests(unittest.TestCase):
    def test_run116_authored_query_uses_no_new_provider_call(self) -> None:
        query = _validate_alternate_query(
            {"alternate_query": RUN116_AUTHORED_ALTERNATE},
            original_query="person sitting cluttered desk crumpled papers and laptop",
            planned=True,
        )["alternate_query"]
        self.assertEqual(query, RUN116_AUTHORED_ALTERNATE)
        with self.assertRaises(AlternateQueryError):
            _validate_alternate_query(
                {"alternate_query": RUN116_AUTHORED_ALTERNATE},
                original_query="person sitting cluttered desk crumpled papers and laptop",
            )

    def test_planned_query_still_bounded_and_non_identical(self) -> None:
        for query in ("same stock query", "word " * 40):
            with self.subTest(query=query):
                with self.assertRaises(AlternateQueryError):
                    _validate_alternate_query(
                        {"alternate_query": query},
                        original_query="same stock query",
                        planned=True,
                    )

    def test_run116_candidates_spread_between_available_sources(self) -> None:
        # Run116 picked three Pixabay candidates despite available Pexels results.
        ranked = [
            {"provider": "pixabay", "asset_id": "6395"},
            {"provider": "pixabay", "asset_id": "31392"},
            {"provider": "pixabay", "asset_id": "22354"},
            {"provider": "pexels", "asset_id": "p1"},
            {"provider": "pexels", "asset_id": "p2"},
        ]
        result = _diversify_recovery_candidates(ranked, limit=3)
        self.assertEqual(result[0], ranked[0])
        self.assertEqual(result[1], ranked[3])
        self.assertEqual(result[2], ranked[1])
        self.assertCountEqual(result, ranked)
        self.assertEqual(_diversify_recovery_candidates(ranked, limit=1), ranked)

    def test_one_source_keeps_original_order_and_no_extra_candidates(self) -> None:
        ranked = [{"provider": "pixabay", "asset_id": str(i)} for i in range(4)]
        self.assertEqual(_diversify_recovery_candidates(ranked, limit=3), ranked)


if __name__ == "__main__":
    unittest.main()
