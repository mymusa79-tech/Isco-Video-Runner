from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2 import music_library as music


class CleanV2CoverrMusicTests(unittest.TestCase):
    def test_coverr_candidate_filter_accepts_calm_instrumental_and_rejects_vocals(self) -> None:
        safe = {
            "id": "safe-1",
            "title": "Quiet Progress",
            "tags": ["Piano", "Ambient", "Peaceful"],
            "mp3_url": "https://cdn.coverr.co/audio/quiet-progress.mp3",
        }
        vocal = {
            "id": "vocal-1",
            "title": "Voice Track",
            "tags": ["Piano", "Backing Vocals", "Hopeful"],
            "mp3_url": "https://cdn.coverr.co/audio/voice-track.mp3",
        }
        premium = {
            **safe,
            "id": "premium-1",
            "isPremium": True,
        }
        generated = {
            **safe,
            "id": "ai-1",
            "is_ai": True,
        }

        self.assertTrue(music._coverr_music_candidate_ok(safe))
        self.assertFalse(music._coverr_music_candidate_ok(vocal))
        self.assertFalse(music._coverr_music_candidate_ok(premium))
        self.assertFalse(music._coverr_music_candidate_ok(generated))

    def test_coverr_download_parser_prefers_mp3_and_never_accepts_external_host(self) -> None:
        item = {
            "files": {
                "wav": "https://cdn.coverr.co/audio/example.wav",
                "mp3": "https://cdn.coverr.co/audio/example.mp3",
            }
        }
        self.assertEqual(
            music._coverr_audio_download(item),
            ("https://cdn.coverr.co/audio/example.mp3", ".mp3"),
        )
        self.assertIsNone(
            music._coverr_audio_download(
                {"mp3_url": "https://example.com/example.mp3"}
            )
        )

    def test_coverr_search_uses_documented_audios_query_contract(self) -> None:
        response = {
            "hits": [],
            "page": 0,
            "pages": 0,
            "page_size": 12,
            "total": 0,
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                import json
                return json.dumps(response).encode("utf-8")

        seen = {}

        def fake_urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["authorization"] = request.headers.get("Authorization")
            seen["timeout"] = timeout
            return FakeResponse()

        with mock.patch.object(music, "_read_secret", return_value="secret"), mock.patch.object(
            music.urllib.request,
            "urlopen",
            side_effect=fake_urlopen,
        ):
            path, report = music._try_coverr_music(
                family="focus",
                fmt="film",
                allow_download=True,
            )

        self.assertIsNone(path)
        self.assertEqual(report["status"], "no_safe_instrumental_candidate")
        self.assertEqual(report["provider_calls_added"], 1)
        self.assertIn("https://api.coverr.co/audios?", seen["url"])
        self.assertIn("query=piano", seen["url"])
        self.assertIn("page_size=12", seen["url"])
        self.assertIn("sort=popular", seen["url"])
        self.assertEqual(seen["authorization"], "Bearer secret")

    def test_production_prefers_safe_coverr_track_then_keeps_freepd_as_fallback(self) -> None:
        script = {
            "title": "التركيز في العمل",
            "sections": [{"narration": "خطوة عملية تساعدك على التركيز في العمل."}],
        }
        with tempfile.TemporaryDirectory() as root:
            coverr_path = Path(root) / "coverr.mp3"
            coverr_path.write_bytes(b"ID3" + b"x" * 2048)
            coverr_report = {
                "provider": "coverr",
                "status": "selected",
                "provider_calls_added": 1,
                "selected_id": "coverr-1",
                "selected_title": "Quiet Progress",
                "selected_instrumental_only": True,
                "selected_dialogue_bed": True,
                "license": "Coverr free stock music",
                "license_url": music.COVERR_MUSIC_LICENSE_URL,
            }
            with mock.patch.object(
                music,
                "_try_coverr_music",
                return_value=(coverr_path, coverr_report),
            ), mock.patch.object(music, "ensure_music_library") as local_library:
                selected, report = music.select_music_track(
                    script,
                    fmt="film",
                    allow_download=True,
                )

            self.assertEqual(selected, coverr_path)
            self.assertEqual(report["source"], "coverr")
            self.assertEqual(report["selected_origin"], "coverr_free_stock_music")
            self.assertTrue(report["selected_instrumental_only"])
            self.assertTrue(report["selected_dialogue_bed"])
            self.assertEqual(report["provider_calls_added"], 1)
            local_library.assert_not_called()

    def test_coverr_miss_falls_back_to_existing_verified_dialogue_bed(self) -> None:
        script = {
            "title": "التركيز في العمل",
            "sections": [{"narration": "نرتب مهمة واحدة ونبدأ بها."}],
        }
        with tempfile.TemporaryDirectory() as root:
            local_path = Path(root) / "local.mp3"
            local_path.write_bytes(b"ID3" + b"x" * 2048)
            fallback_report = {
                "source": "FreePD",
                "license": "CC0",
                "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                "ready": [
                    {
                        "id": "calm-sketch-piano",
                        "title": "Calm Sketch Piano",
                        "path": str(local_path),
                    }
                ],
                "unavailable": [],
                "allow_download": True,
                "requested_track_ids": ["calm-sketch-piano"],
                "catalog_track_count": 6,
            }
            coverr_report = {
                "provider": "coverr",
                "status": "no_safe_instrumental_candidate",
                "provider_calls_added": 1,
            }
            with mock.patch.object(
                music,
                "_try_coverr_music",
                return_value=(None, coverr_report),
            ), mock.patch.object(
                music,
                "ensure_music_library",
                return_value=fallback_report,
            ):
                selected, report = music.select_music_track(
                    script,
                    fmt="short",
                    allow_download=True,
                )

            self.assertEqual(selected, local_path)
            self.assertEqual(
                report["selected_origin"],
                "verified_cc0_freepd_instrumental_dialogue_bed",
            )
            self.assertEqual(report["provider_calls_added"], 1)
            self.assertEqual(report["coverr"]["status"], "no_safe_instrumental_candidate")

    def test_legacy_call_never_spends_coverr_request(self) -> None:
        with mock.patch.object(music, "_try_coverr_music") as coverr, mock.patch.object(
            music,
            "ensure_music_library",
            return_value={
                "source": "FreePD",
                "license": "CC0",
                "license_url": "",
                "ready": [],
                "unavailable": [],
                "allow_download": False,
                "requested_track_ids": [],
                "catalog_track_count": 6,
            },
        ):
            _path, report = music.select_music_track(
                {"title": "اختبار", "sections": []},
                allow_download=False,
            )
        coverr.assert_not_called()
        self.assertEqual(report["provider_calls_added"], 0)


if __name__ == "__main__":
    unittest.main()
