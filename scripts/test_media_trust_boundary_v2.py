from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.media_trust_boundary_v2 as trust


class _Response:
    def __init__(self, *, status=200, headers=None, chunks=None):
        self.status_code = status
        self.headers = dict(headers or {})
        self._chunks = list(chunks or [])
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1024 * 1024):
        del chunk_size
        yield from self._chunks

    def close(self):
        self.closed = True


class MediaTrustBoundaryV2Tests(unittest.TestCase):
    def setUp(self):
        trust.reset_media_trust_state_for_tests()

    def tearDown(self):
        trust.reset_media_trust_state_for_tests()

    def test_review_variant_is_exact_render_variant(self):
        candidate = {
            "video_files": [
                {"link": "https://videos.pexels.com/small.mp4", "width": 640, "height": 360},
                {"link": "https://videos.pexels.com/full.mp4", "width": 1920, "height": 1080},
                {"link": "https://videos.pexels.com/portrait.mp4", "width": 720, "height": 1280},
            ]
        }
        landscape = trust._review_exact_render_variant(candidate, portrait=False)
        portrait = trust._review_exact_render_variant(candidate, portrait=True)
        self.assertEqual(landscape, trust.pexels_provider.best_file(candidate, portrait=False))
        self.assertEqual(portrait, trust.pexels_provider.best_file(candidate, portrait=True))
        self.assertEqual(landscape["link"], "https://videos.pexels.com/full.mp4")
        self.assertEqual(portrait["link"], "https://videos.pexels.com/portrait.mp4")

    def test_manual_same_provider_redirect_is_revalidated_and_followed_once(self):
        calls = []
        first = _Response(status=302, headers={"location": "https://cdn.pexels.com/final.mp4"})
        second = _Response(
            headers={"content-type": "video/mp4", "content-length": "6"},
            chunks=[b"abc", b"123"],
        )

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            return first if len(calls) == 1 else second

        with tempfile.TemporaryDirectory() as td, patch.object(trust, "_http_get", fake_get):
            dest = Path(td) / "media.mp4"
            result = trust.trusted_download("pexels", "https://videos.pexels.com/start.mp4", dest)
            self.assertEqual(result.read_bytes(), b"abc123")
            self.assertEqual([url for url, _ in calls], [
                "https://videos.pexels.com/start.mp4",
                "https://cdn.pexels.com/final.mp4",
            ])
            self.assertTrue(all(call[1]["allow_redirects"] is False for call in calls))
            record = trust.trusted_record(result)
            self.assertIsNotNone(record)
            self.assertEqual(record.final_url, "https://cdn.pexels.com/final.mp4")
            self.assertEqual(record.sha256, hashlib.sha256(b"abc123").hexdigest())

    def test_redirect_to_unapproved_host_is_blocked_before_second_request(self):
        calls = []
        first = _Response(status=302, headers={"location": "https://evil.example/payload.mp4"})

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            return first

        with tempfile.TemporaryDirectory() as td, patch.object(trust, "_http_get", fake_get):
            with self.assertRaisesRegex(RuntimeError, "media_trust_unapproved_pexels_url"):
                trust.trusted_download(
                    "pexels", "https://videos.pexels.com/start.mp4", Path(td) / "media.mp4"
                )
        self.assertEqual(len(calls), 1)

    def test_download_once_then_materialize_same_hash_without_second_network_call(self):
        calls = []
        payload = b"immutable-stock-bytes"

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            return _Response(
                headers={"content-type": "video/mp4", "content-length": str(len(payload))},
                chunks=[payload],
            )

        with tempfile.TemporaryDirectory() as td, patch.object(trust, "_http_get", fake_get):
            root = Path(td)
            first = trust.trusted_download("pexels", "https://videos.pexels.com/a.mp4", root / "review.mp4")
            second = trust.trusted_download("pexels", "https://videos.pexels.com/a.mp4", root / "render.mp4")
            self.assertEqual(len(calls), 1)
            self.assertEqual(first.read_bytes(), payload)
            self.assertEqual(second.read_bytes(), payload)
            self.assertEqual(trust.trusted_record(first).sha256, trust.trusted_record(second).sha256)

    def test_quarantine_tampering_fails_closed_before_reuse(self):
        payload = b"trusted"

        def fake_get(url, **kwargs):
            del url, kwargs
            return _Response(
                headers={"content-type": "video/mp4", "content-length": str(len(payload))},
                chunks=[payload],
            )

        with tempfile.TemporaryDirectory() as td, patch.object(trust, "_http_get", fake_get):
            root = Path(td)
            first = trust.trusted_download("pexels", "https://videos.pexels.com/a.mp4", root / "review.mp4")
            record = trust.trusted_record(first)
            record.quarantine_path.write_bytes(b"tampered")
            with self.assertRaisesRegex(RuntimeError, "media_trust_quarantine_hash_mismatch"):
                trust.trusted_download("pexels", "https://videos.pexels.com/a.mp4", root / "render.mp4")
            self.assertFalse((root / "render.mp4").exists())

    def test_oversize_declared_download_never_promotes_destination(self):
        response = _Response(
            headers={
                "content-type": "video/mp4",
                "content-length": str(trust.MAX_DOWNLOAD_BYTES + 1),
            },
            chunks=[b"x"],
        )
        with tempfile.TemporaryDirectory() as td, patch.object(trust, "_http_get", lambda *a, **k: response):
            dest = Path(td) / "media.mp4"
            with self.assertRaisesRegex(RuntimeError, "media_trust_download_size_limit"):
                trust.trusted_download("pexels", "https://videos.pexels.com/a.mp4", dest)
            self.assertFalse(dest.exists())
            self.assertFalse(any(Path(td).glob("*.tmp")))

    def test_distributed_timestamps_cover_beginning_middle_and_end(self):
        samples = trust._sample_timestamps(60.0)
        self.assertGreaterEqual(len(samples), 3)
        self.assertLessEqual(len(samples), trust.VIDEO_MAX_SAMPLES)
        self.assertEqual(samples[0], 0.0)
        self.assertGreater(samples[-1], 59.0)
        self.assertTrue(any(15.0 < value < 45.0 for value in samples))

        long_samples = trust._sample_timestamps(600.0)
        self.assertEqual(len(long_samples), trust.VIDEO_MAX_SAMPLES)
        self.assertGreater(long_samples[-1], 599.0)

    def test_preflight_on_preview_is_redirected_to_exact_raw_source(self):
        seen = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw = root / "raw.mp4"
            preview = root / "preview.mp4"
            raw.write_bytes(b"raw")
            preview.write_bytes(b"preview")
            trust._register_preview(preview, raw)

            def fake_inspect(path):
                seen.append(Path(path))
                return None

            with patch.object(trust.stock_preflight, "inspect_stock_media", fake_inspect):
                self.assertIsNone(trust._inspect_exact_review_source(preview))
        self.assertEqual(seen, [raw])

    def test_distributed_scanner_extracts_frames_across_full_source(self):
        seek_values = []

        class FakeFirewall:
            def __init__(self, *, ocr_backend):
                self.ocr_backend = ocr_backend

            def scan_frame(self, frame):
                return {"frame": str(frame)}

        def fake_run(command, **kwargs):
            del kwargs
            output = Path(command[-1])
            output.write_bytes(b"P5\n1 1\n255\n\x00")
            if "-ss" in command:
                seek_values.append(float(command[command.index("-ss") + 1]))
            class Result:
                returncode = 0
                stdout = ""
                stderr = ""
            return Result()

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "source.mp4"
            source.write_bytes(b"not-real-media-but-scanner-subprocess-is-mocked")
            with (
                patch.object(trust.shutil, "which", lambda name: f"/usr/bin/{name}"),
                patch.object(trust, "_probe_duration", lambda source, ffprobe: 60.0),
                patch.object(trust.subprocess, "run", fake_run),
                patch.object(trust.security_v1, "MultimodalInjectionFirewall", FakeFirewall),
                patch.object(trust.security_v1, "require_normal_vision_safe", lambda result: result),
            ):
                trust._distributed_scan_media_before_vision(source)
        self.assertEqual(seek_values[0], 0.0)
        self.assertGreater(seek_values[-1], 59.0)
        self.assertTrue(any(15.0 < value < 45.0 for value in seek_values))


if __name__ == "__main__":
    unittest.main()
