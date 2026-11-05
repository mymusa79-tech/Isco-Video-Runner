"""Guard the same explicit Claude-reviewed ZIP -> Telegram approval -> Clean V2 production path.

These are offline safety contract checks, not a substitute for actual human/
Claude examination of a stock clip, script, images, and rights.
"""
from __future__ import annotations

import copy
import unittest
from pathlib import Path

from clean_v2.production_package import validate_manifest, inspect_archive
from scripts.test_production_package import archive_bytes, manifest
from scripts.telegram_clean_v2_control import _request_hash

ROOT = Path(__file__).resolve().parents[1]
STOCK = "https://www.pexels.com/video/a-man-walking-down-a-path-in-the-woods-28464399/"


def cinematic(style: str) -> dict:
    doc = manifest()
    doc["visual_style"] = style
    doc["scenes"][1]["stock"] = {"url": STOCK}
    return doc


class CinematicReleaseGateTests(unittest.TestCase):
    def test_both_modes_require_the_same_claude_review_marker_as_legacy(self):
        for style in ("color_cinematic", "bw_cinematic"):
            with self.subTest(style=style):
                doc = cinematic(style)
                self.assertEqual(validate_manifest(doc)["approval"], {
                    "status": "approved", "reviewer": "Claude"
                })
                self.assertEqual(inspect_archive(archive_bytes(doc))["manifest"]["visual_style"], style)
                for invalid in (
                    {"status": "draft", "reviewer": "Claude"},
                    {"status": "approved", "reviewer": "self"},
                    {"status": "approved", "reviewer": ""},
                    {},
                ):
                    damaged = copy.deepcopy(doc)
                    damaged["approval"] = invalid
                    with self.subTest(style=style, invalid=invalid), self.assertRaises(Exception):
                        validate_manifest(damaged)

    def test_approved_pack_hash_covers_style_and_claude_marker(self):
        doc = cinematic("color_cinematic")
        request = {
            "schema_version": 1,
            "request_id": "local-proof",
            "source": "telegram_clean_v2",
            "scope": "short",
            "approved_by_user": True,
            "owner_pack": {"production": doc, "zip_sha256": "a" * 64},
        }
        original = _request_hash(request)
        different = copy.deepcopy(request)
        different["owner_pack"]["production"]["visual_style"] = "bw_cinematic"
        self.assertNotEqual(original, _request_hash(different))
        different = copy.deepcopy(request)
        different["owner_pack"]["production"]["approval"]["status"] = "draft"
        self.assertNotEqual(original, _request_hash(different))
        different = copy.deepcopy(request)
        different["owner_pack"]["production"]["scenes"][1]["stock"]["url"] = (
            "https://www.pexels.com/video/some-other-clip-9999999/"
        )
        self.assertNotEqual(original, _request_hash(different))

    def test_existing_telegram_production_keeps_cairo_before_voice(self):
        script = (ROOT / ".github/workflows/clean-v2-telegram-production.yml").read_text(encoding="utf-8")
        self.assertLess(
            script.index("bash scripts/install_cairo_bold.sh"),
            script.index("- name: Produce exact approved Telegram scope"),
        )
        self.assertIn('test "$(jq -r .status "$out/run-manifest.json")" = "pass"', script)
        self.assertIn('test "$(jq -r .status "$out/final-master-qc.json")" = "pass"', script)
        self.assertIn('CLEAN_V2_OWNER_PACK=$owner_pack', script)

    def test_production_requires_manual_telegram_confirmation(self):
        script = (ROOT / ".github/workflows/telegram-clean-v2-control.yml").read_text(encoding="utf-8")
        self.assertIn("Dispatch Clean V2 only after request-bound confirmation", script)
        self.assertIn("request_sha256", script)
        short = manifest()
        self.assertNotIn("visual_style", short)
        self.assertEqual(validate_manifest(short), short)


if __name__ == "__main__":
    unittest.main()
