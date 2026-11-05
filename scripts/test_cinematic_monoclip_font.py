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


    def test_wrapper_prepares_font_before_any_production_execution(self):
        from scripts import run_telegram_control_production as wrapper

        calls = []
        with mock.patch.dict("os.environ", {
            "CONTROL_PLANE_PRODUCTION_ENABLED": "true",
            "ISCO_CONTROL_REQUEST_PATH": "/tmp/nonexistent-control-request.json",
            "ISCO_CONTROL_REQUEST_SHA256": "f" * 64,
            "ISCO_CONTROL_RELEASE_TAG": "local-unpublished-test",
        }), mock.patch.object(
            wrapper, "load_control_request", return_value=owner_request()
        ), mock.patch(
            "scripts.cinematic_monoclip_font.prepare_cinematic_cairo",
            side_effect=lambda _: calls.append("font"),
        ), mock.patch.object(
            wrapper, "execute_bundle",
            side_effect=lambda *a, **kw: (calls.append("execute"), (_ for _ in ()).throw(RuntimeError("stop_before_paid_production")))[1],
        ):
            with self.assertRaisesRegex(RuntimeError, "stop_before_paid_production"):
                wrapper.main()
        self.assertEqual(calls, ["font", "execute"])

    def test_telegram_confirmation_shows_prayerless_opt_in_only(self):
        from scripts.telegram_clean_v2_control import render_owner_pack_confirmation
        from scripts.test_production_package import manifest

        doc = manifest()
        normal = render_owner_pack_confirmation({
            "owner_pack": {"production": doc, "zip_sha256": "f" * 64}
        })
        self.assertIn("تُضاف هوية النوع الحالية والصلاة", normal)
        doc["visual_style"] = "color_cinematic"
        doc["scenes"][1]["stock"] = {
            "url": "https://www.pexels.com/video/a-man-walking-down-a-path-in-the-woods-28464399/"
        }
        mono = render_owner_pack_confirmation({
            "owner_pack": {"production": doc, "zip_sha256": "f" * 64}
        })
        self.assertIn("بلا صلاة أو انترو أو أوترو", mono)
        self.assertNotIn("تُضاف هوية النوع الحالية والصلاة", mono)


if __name__ == "__main__":
    unittest.main()
