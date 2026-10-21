from __future__ import annotations

import unittest

from clean_v2.short_timed_text import (
    _CAPTION_FONT_MIN,
    _caption_rows_and_size,
)


class ShortMobileCaptionFloorTests(unittest.TestCase):
    def test_short_floor_is_readable_and_landscape_unchanged(self) -> None:
        self.assertGreaterEqual(_CAPTION_FONT_MIN["short"], 120)
        self.assertEqual(_CAPTION_FONT_MIN["film"], 64)
        self.assertEqual(_CAPTION_FONT_MIN["podcast"], 40)

    def test_long_short_phrase_never_shrinks_below_mobile_floor(self) -> None:
        rows, size = _caption_rows_and_size(
            "كان يستطيع إصلاح الفرن فورًا لكنه اختار إعادة الظرف إلى صاحبه",
            fmt="short",
            font_size=168,
        )
        self.assertLessEqual(len(rows), 2)
        self.assertGreaterEqual(size, 120)


if __name__ == "__main__":
    unittest.main()
