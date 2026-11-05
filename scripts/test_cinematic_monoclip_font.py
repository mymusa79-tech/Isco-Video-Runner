"""No-cost, isolated Cairo installation tests for approved Monoclip only."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2.short_timed_text import ShortTimedTextError
from scripts.cinematic_monoclip_font import (
    prepare_cinematic_cairo, requires_cinematic_cairo,
)

MISSING = ShortTimedTextError("monoclip_cairo_bold_font_not_installed")


def owner_request(style="color_cinematic"):
    return {"owner_pack": {"production": {"format": "short", "visual_style": style}}}


class MonoclipCairoPreflightTests(unittest.TestCase):
    def test_old_short_film_podcast_and_ala_mahl_cannot_install_font(self):
        requests = [
            {},
            {"owner_pack": {"production": {"format": "short"}}},
            {"owner_pack": {"production": {"format": "film", "visual_style": "bw_cinematic"}}},
            {"owner_pack": {"production": {"format": "podcast", "visual_style": "color_cinematic"}}},
            {"owner_pack": {"production": {"format": "short", "template": "ala_mahl"}}},
        ]
        with mock.patch("scripts.cinematic_monoclip_font.subprocess.run") as command, mock.patch(
            "scripts.cinematic_monoclip_font._require_monoclip_cairo_font"
        ) as check:
            for request in requests:
                with self.subTest(request=request):
                    self.assertFalse(requires_cinematic_cairo(request))
                    self.assertEqual(prepare_cinematic_cairo(request), "not_applicable")
        command.assert_not_called()
        check.assert_not_called()

    def test_preinstalled_cairo_does_not_trigger_download(self):
        with mock.patch("scripts.cinematic_monoclip_font.subprocess.run") as command, mock.patch(
            "scripts.cinematic_monoclip_font._require_monoclip_cairo_font"
        ) as check:
            self.assertEqual(prepare_cinematic_cairo(owner_request()), "installed")
        command.assert_not_called()
        check.assert_called_once()

    def test_missing_cairo_downloads_only_for_monoclip_and_rechecks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def fake_run(argv, **kwargs):
                if argv[0] == "curl":
                    Path(argv[-1]).write_bytes(b"x" * 150000)
                    return subprocess.CompletedProcess(argv, 0)
                if argv[0] == "fc-scan":
                    return subprocess.CompletedProcess(argv, 0, stdout="Cairo")
                if argv[0] == "fc-cache":
                    return subprocess.CompletedProcess(argv, 0)
                raise AssertionError("Unexpected command: " + str(argv))

            with mock.patch("scripts.cinematic_monoclip_font.Path.home", return_value=root), mock.patch(
                "scripts.cinematic_monoclip_font._require_monoclip_cairo_font",
                side_effect=[MISSING, None],
            ) as check, mock.patch(
                "scripts.cinematic_monoclip_font.subprocess.run", side_effect=fake_run
            ) as command:
                self.assertEqual(prepare_cinematic_cairo(owner_request("bw_cinematic")), "prepared")
                self.assertEqual(command.call_count, 3)
                self.assertEqual(check.call_count, 2)
                self.assertTrue(
                    (root / ".local/share/fonts/isco-cairo/Cairo.ttf").is_file()
                )

    def test_font_validation_failure_stops_before_voice_or_install(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def bad_run(argv, **kwargs):
                if argv[0] == "curl":
                    Path(argv[-1]).write_bytes(b"x" * 150000)
                    return subprocess.CompletedProcess(argv, 0)
                if argv[0] == "fc-scan":
                    return subprocess.CompletedProcess(argv, 0, stdout="Noto Sans")
                raise AssertionError("Invalid font must never be installed")

            with mock.patch("scripts.cinematic_monoclip_font.Path.home", return_value=root), mock.patch(
                "scripts.cinematic_monoclip_font._require_monoclip_cairo_font", side_effect=MISSING
            ), mock.patch("scripts.cinematic_monoclip_font.subprocess.run", side_effect=bad_run):
                with self.assertRaisesRegex(RuntimeError, "download_family_invalid"):
                    prepare_cinematic_cairo(owner_request())
            self.assertFalse((root / ".local/share/fonts/isco-cairo/Cairo.ttf").exists())


if __name__ == "__main__":
    unittest.main()
