"""The legacy grey M10 evidence card must not repeat caption text on podcast / long."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clean_v2 import legacy_cinematic as lc  # noqa: E402

POLICY = {"status": "planned", "cards": [{"kind": "quote", "primary_text": "مغالطة التكلفة الغارقة",
                                           "start_seconds": 60.0, "end_seconds": 66.0}]}


class GreyCardTests(unittest.TestCase):
    def test_podcast_and_film_never_draw_the_card(self):
        for fmt in ("podcast", "film"):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(lc, "_apply_m10_cards") as render:
                out = lc._m10_cards_for_format(fmt, Path(tmp) / "f.mp4", dict(POLICY), output_dir=Path(tmp))
                render.assert_not_called()
                self.assertEqual(out["cards"], [])
                self.assertEqual(out["cards_suppressed"], 1)
                self.assertTrue(out["status"].startswith("skipped"))

    def test_other_formats_keep_the_card(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(lc, "_apply_m10_cards", return_value={"ok": 1}) as render:
            self.assertEqual(lc._m10_cards_for_format("short", Path(tmp) / "f.mp4", dict(POLICY), output_dir=Path(tmp)), {"ok": 1})
            render.assert_called_once()


if __name__ == "__main__":
    unittest.main()
