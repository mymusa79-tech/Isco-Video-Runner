from __future__ import annotations

import unittest
from pathlib import Path

from clean_v2.visual_qa import CleanV2VisualQABlock, _bind_selected_rows


def _plan(count: int) -> dict:
    return {
        "sections": [
            {
                "id": f"s{index}",
                "visual_query_en": f"visual {index}",
            }
            for index in range(1, count + 1)
        ]
    }


class CleanV2VisualQABindingTests(unittest.TestCase):
    def test_six_section_plan_with_five_rendered_clips_is_valid(self) -> None:
        clips = [Path(f"visual-{index:02d}.mp4") for index in range(1, 6)]
        rights = [
            {
                "section_id": f"s{index}",
                "local_file": clips[index - 1].name,
                "provider": "fixture",
                "asset_id": str(index),
            }
            for index in range(1, 6)
        ]

        rows = _bind_selected_rows(
            clips=clips,
            rights=rights,
            plan=_plan(6),
        )

        self.assertEqual(len(rows), 5)
        self.assertEqual(
            [row[2]["id"] for row in rows],
            ["s1", "s2", "s3", "s4", "s5"],
        )

    def test_clip_rights_cardinality_mismatch_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            CleanV2VisualQABlock,
            "clip_rights_cardinality_mismatch",
        ):
            _bind_selected_rows(
                clips=[Path("a.mp4"), Path("b.mp4")],
                rights=[
                    {
                        "section_id": "s1",
                        "local_file": "a.mp4",
                        "provider": "fixture",
                        "asset_id": "1",
                    }
                ],
                plan=_plan(2),
            )

    def test_unknown_or_duplicate_section_binding_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            CleanV2VisualQABlock,
            "selected_visual_section_binding_invalid",
        ):
            _bind_selected_rows(
                clips=[Path("a.mp4")],
                rights=[
                    {
                        "section_id": "missing",
                        "local_file": "a.mp4",
                        "provider": "fixture",
                        "asset_id": "1",
                    }
                ],
                plan=_plan(2),
            )

        with self.assertRaisesRegex(
            CleanV2VisualQABlock,
            "selected_visual_section_binding_invalid",
        ):
            _bind_selected_rows(
                clips=[Path("a.mp4"), Path("b.mp4")],
                rights=[
                    {
                        "section_id": "s1",
                        "local_file": "a.mp4",
                        "provider": "fixture",
                        "asset_id": "1",
                    },
                    {
                        "section_id": "s1",
                        "local_file": "b.mp4",
                        "provider": "fixture",
                        "asset_id": "2",
                    },
                ],
                plan=_plan(2),
            )


if __name__ == "__main__":
    unittest.main()
