from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import isco_video_agent.media.ffmpeg as media_ffmpeg
import isco_video_agent.orchestrator as orchestrator

import scripts.m8_live_binding as m8
import scripts.media_durable_asset_cache as cache
import scripts.media_trust_boundary_v2 as media_trust


class MediaDurableAssetCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache_dir = self.root / "cache"
        self.run1 = self.root / "output" / "run-1"
        self.run2 = self.root / "output" / "run-2"
        self.original_trusted_download = media_trust.trusted_download
        self.original_prepare_clip = orchestrator.prepare_clip
        self.original_cache_download = cache._original_trusted_download
        self.original_cache_prepare = cache._original_prepare_clip
        self.original_qroot = media_trust._quarantine_root
        self.original_records_url = dict(media_trust._records_by_url)
        self.original_records_path = dict(media_trust._records_by_path)
        self.network_calls = 0
        self.render_calls = 0

        media_trust._records_by_url.clear()
        media_trust._records_by_path.clear()
        media_trust._quarantine_root = None
        cache._original_trusted_download = None
        cache._original_prepare_clip = None

        def fake_download(provider: str, url: str, dest: Path) -> Path:
            self.network_calls += 1
            destination = Path(dest)
            destination.parent.mkdir(parents=True, exist_ok=True)
            payload = (
                f"raw-{provider}-{url}-{self.network_calls}".encode("utf-8") + b"x" * 8192
            )
            destination.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            record = media_trust.TrustedMediaRecord(
                provider=str(provider),
                source_url=str(url),
                final_url=str(url),
                sha256=digest,
                byte_length=len(payload),
                quarantine_path=destination,
            )
            media_trust._records_by_url[(str(provider), str(url))] = record
            media_trust._records_by_path[media_trust._path_key(destination)] = record
            return destination

        def fake_prepare(
            src: Path, dest: Path, seconds: float, portrait: bool, fps: int = 30
        ) -> Path:
            self.render_calls += 1
            destination = Path(dest)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(
                b"prepared-"
                + Path(src).read_bytes()
                + f"-{seconds}-{portrait}-{fps}".encode("utf-8")
            )
            return destination

        media_trust.trusted_download = fake_download
        orchestrator.prepare_clip = fake_prepare

        self.env = patch.dict(
            os.environ, {"ISCO_MEDIA_CACHE_DIR": str(self.cache_dir)}, clear=False
        )
        self.env.start()
        self.trust_hash = patch.object(
            cache, "_media_trust_contract_sha256", return_value="trust-v1"
        )
        self.render_hash = patch.object(
            cache, "_render_contract_sha256", return_value="render-v1"
        )
        self.ffmpeg_id = patch.object(cache, "_ffmpeg_identity", return_value="ffmpeg-test")
        self.prepared_validation = patch.object(cache, "_validate_prepared_clip")
        self.trust_hash.start()
        self.render_hash.start()
        self.ffmpeg_id.start()
        self.prepared_validation.start()

        cache.install_media_durable_asset_cache()

    def tearDown(self) -> None:
        media_trust.trusted_download = self.original_trusted_download
        orchestrator.prepare_clip = self.original_prepare_clip
        cache._original_trusted_download = self.original_cache_download
        cache._original_prepare_clip = self.original_cache_prepare
        if (
            media_trust._quarantine_root is not None
            and media_trust._quarantine_root != self.original_qroot
        ):
            try:
                import shutil

                shutil.rmtree(media_trust._quarantine_root, ignore_errors=True)
            except OSError:
                pass
        media_trust._quarantine_root = self.original_qroot
        media_trust._records_by_url.clear()
        media_trust._records_by_url.update(self.original_records_url)
        media_trust._records_by_path.clear()
        media_trust._records_by_path.update(self.original_records_path)
        self.prepared_validation.stop()
        self.ffmpeg_id.stop()
        self.render_hash.stop()
        self.trust_hash.stop()
        self.env.stop()
        self.tmp.cleanup()

    def _clear_live_trust(self) -> None:
        media_trust._records_by_url.clear()
        media_trust._records_by_path.clear()
        if media_trust._quarantine_root is not None:
            import shutil

            shutil.rmtree(media_trust._quarantine_root, ignore_errors=True)
            media_trust._quarantine_root = None

    def test_selected_raw_asset_resumes_without_second_network_call_and_rehydrates_trust(self) -> None:
        url = "https://images.pexels.com/videos/example.mp4"
        first = media_trust.trusted_download(
            "pexels", url, self.run1 / "clips" / "01-raw.mp4"
        )
        first_bytes = first.read_bytes()
        self.assertEqual(self.network_calls, 1)

        self._clear_live_trust()
        second = media_trust.trusted_download(
            "pexels", url, self.run2 / "visual-review" / "01-pexels-I01-source.mp4"
        )

        self.assertEqual(self.network_calls, 1)
        self.assertEqual(second.read_bytes(), first_bytes)
        record = media_trust.trusted_record(second)
        self.assertIsNotNone(record)
        self.assertEqual(record.sha256, hashlib.sha256(first_bytes).hexdigest())
        audit = json.loads(
            (self.run2 / cache.AUDIT_FILENAME).read_text(encoding="utf-8")
        )
        self.assertEqual(audit["summary"]["raw_hits"], 1)

    def test_rejected_review_candidate_is_not_persisted(self) -> None:
        url = "https://videos.pexels.com/video-files/rejected.mp4"
        media_trust.trusted_download(
            "pexels", url, self.run1 / "visual-review" / "01-pexels-I01-source.mp4"
        )
        raw_root = self.cache_dir / "raw"
        self.assertFalse(raw_root.exists() and any(raw_root.iterdir()))

    def test_corrupt_raw_entry_is_evicted_and_reacquired(self) -> None:
        url = "https://videos.pexels.com/video-files/selected.mp4"
        media_trust.trusted_download(
            "pexels", url, self.run1 / "clips" / "01-raw.mp4"
        )
        fingerprint, _ = cache.raw_fingerprint(provider="pexels", source_url=url)
        payload = self.cache_dir / "raw" / fingerprint / cache.RAW_FILENAME
        payload.write_bytes(b"tampered")
        self._clear_live_trust()

        media_trust.trusted_download(
            "pexels", url, self.run2 / "clips" / "01-raw.mp4"
        )

        self.assertEqual(self.network_calls, 2)
        audit = json.loads(
            (self.run2 / cache.AUDIT_FILENAME).read_text(encoding="utf-8")
        )
        self.assertEqual(audit["summary"]["raw_invalidated"], 1)
        self.assertEqual(audit["summary"]["raw_stored"], 1)

    def test_prepared_clip_resumes_without_second_render(self) -> None:
        url = "https://videos.pexels.com/video-files/render.mp4"
        raw1 = media_trust.trusted_download(
            "pexels", url, self.run1 / "clips" / "01-raw.mp4"
        )
        prepared1 = orchestrator.prepare_clip(
            raw1, self.run1 / "clips" / "01-prepared.mp4", 12.0, False, fps=30
        )
        expected = prepared1.read_bytes()
        self.assertEqual(self.render_calls, 1)

        self._clear_live_trust()
        raw2 = media_trust.trusted_download(
            "pexels", url, self.run2 / "clips" / "01-raw.mp4"
        )
        prepared2 = orchestrator.prepare_clip(
            raw2, self.run2 / "clips" / "01-prepared.mp4", 12.0, False, fps=30
        )

        self.assertEqual(self.network_calls, 1)
        self.assertEqual(self.render_calls, 1)
        self.assertEqual(prepared2.read_bytes(), expected)
        audit = json.loads(
            (self.run2 / cache.AUDIT_FILENAME).read_text(encoding="utf-8")
        )
        self.assertEqual(audit["summary"]["prepared_hits"], 1)

    def test_complete_m8_sidecar_is_restored_with_prepared_hit(self) -> None:
        url = "https://videos.pexels.com/video-files/m8-sidecar.mp4"
        raw1 = media_trust.trusted_download(
            "pexels", url, self.run1 / "clips" / "01-raw.mp4"
        )
        calls = {"n": 0}

        def full_pipeline(
            src: Path, dest: Path, seconds: float, portrait: bool, fps: int = 30
        ) -> Path:
            calls["n"] += 1
            dest = Path(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"m8-final-" + Path(src).read_bytes())
            dest.with_suffix(".m8.json").write_text(
                json.dumps(
                    {
                        "status": "applied",
                        "production_stage": "technical_normalization_before_creative_grade",
                        "fps": fps,
                    }
                ),
                encoding="utf-8",
            )
            return dest

        first = cache.prepare_trusted_clip_with_cache(
            full_pipeline,
            raw1,
            self.run1 / "clips" / "01-prepared.mp4",
            8.0,
            False,
            fps=30,
        )
        expected_video = first.read_bytes()
        expected_sidecar = first.with_suffix(".m8.json").read_text(encoding="utf-8")
        self.assertEqual(calls["n"], 1)

        self._clear_live_trust()
        raw2 = media_trust.trusted_download(
            "pexels", url, self.run2 / "clips" / "01-raw.mp4"
        )
        second = cache.prepare_trusted_clip_with_cache(
            full_pipeline,
            raw2,
            self.run2 / "clips" / "01-prepared.mp4",
            8.0,
            False,
            fps=30,
        )

        self.assertEqual(calls["n"], 1)
        self.assertEqual(second.read_bytes(), expected_video)
        self.assertEqual(
            second.with_suffix(".m8.json").read_text(encoding="utf-8"),
            expected_sidecar,
        )

    def test_m8_live_scope_routes_complete_pipeline_through_durable_cache(self) -> None:
        source = self.run1 / "clips" / "m8-source.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"trusted-source")
        destination = self.run1 / "clips" / "m8-prepared.mp4"
        cache_calls: list[tuple[Path, Path, float, bool, int]] = []

        def fake_normalize(src: Path, dest: Path):
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(b"normalized")
            return object()

        def fake_engine_prepare(
            src: Path, dest: Path, seconds: float, portrait: bool, fps: int = 30
        ) -> Path:
            Path(dest).write_bytes(b"final-m8")
            return Path(dest)

        def fake_cache(
            original,
            src: Path,
            dest: Path,
            seconds: float,
            portrait: bool,
            fps: int = 30,
        ) -> Path:
            cache_calls.append((Path(src), Path(dest), seconds, portrait, fps))
            return original(src, dest, seconds, portrait, fps=fps)

        with patch.object(media_ffmpeg, "prepare_clip", side_effect=fake_engine_prepare), \
             patch.object(m8, "normalize_to_bt709_sdr", side_effect=fake_normalize) as normalize, \
             patch.object(m8, "report_dict", return_value={"normalization": "bt709"}), \
             patch.object(m8, "prepare_trusted_clip_with_cache", side_effect=fake_cache) as durable:
            with m8.m8_live_scope():
                result = orchestrator.prepare_clip(
                    source, destination, 6.0, False, fps=30
                )

        self.assertEqual(result, destination)
        durable.assert_called_once()
        normalize.assert_called_once()
        self.assertEqual(cache_calls, [(source, destination, 6.0, False, 30)])
        evidence = json.loads(destination.with_suffix(".m8.json").read_text(encoding="utf-8"))
        self.assertEqual(evidence["status"], "applied")
        self.assertEqual(
            evidence["production_stage"],
            "technical_normalization_before_creative_grade",
        )

    def test_prepared_semantics_invalidate_duration_orientation_fps_and_source_bytes(self) -> None:
        base = cache.prepared_fingerprint(
            source_sha256="a" * 64, seconds=10.0, portrait=False, fps=30
        )[0]
        variants = {
            cache.prepared_fingerprint(
                source_sha256="a" * 64, seconds=11.0, portrait=False, fps=30
            )[0],
            cache.prepared_fingerprint(
                source_sha256="a" * 64, seconds=10.0, portrait=True, fps=30
            )[0],
            cache.prepared_fingerprint(
                source_sha256="a" * 64, seconds=10.0, portrait=False, fps=24
            )[0],
            cache.prepared_fingerprint(
                source_sha256="b" * 64, seconds=10.0, portrait=False, fps=30
            )[0],
        }
        self.assertEqual(len(variants), 4)
        self.assertNotIn(base, variants)

    def test_cache_hit_does_not_replace_security_or_vision_boundaries(self) -> None:
        inspect_before = orchestrator.inspect_stock_media
        review_before = orchestrator.make_review_preview
        selection_before = orchestrator.select_with_recovery
        cache.install_media_durable_asset_cache()
        self.assertIs(orchestrator.inspect_stock_media, inspect_before)
        self.assertIs(orchestrator.make_review_preview, review_before)
        self.assertIs(orchestrator.select_with_recovery, selection_before)

    def test_prepare_for_persistence_removes_corrupt_and_symlink_entries(self) -> None:
        url = "https://videos.pexels.com/video-files/persist.mp4"
        media_trust.trusted_download(
            "pexels", url, self.run1 / "clips" / "01-raw.mp4"
        )
        bad = self.cache_dir / "prepared" / ("f" * 64)
        bad.mkdir(parents=True)
        (bad / cache.MANIFEST_FILENAME).write_text("{}", encoding="utf-8")
        (bad / cache.PREPARED_FILENAME).write_bytes(b"bad")
        link = self.cache_dir / "raw" / ("e" * 64)
        try:
            link.symlink_to(self.cache_dir / "raw" / "missing", target_is_directory=True)
        except OSError:
            link = None

        self.assertTrue(cache.prepare_cache_for_persistence(self.cache_dir))
        self.assertFalse(bad.exists())
        if link is not None:
            self.assertFalse(link.exists() or link.is_symlink())

    def test_root_symlink_is_rejected(self) -> None:
        target = self.root / "real-cache"
        target.mkdir()
        link = self.root / "cache-link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable")
        with patch.dict(os.environ, {"ISCO_MEDIA_CACHE_DIR": str(link)}, clear=False):
            with self.assertRaises(cache.CacheEntryInvalid):
                cache._configured_cache_root()


class MediaDurableWorkflowContractTests(unittest.TestCase):
    def test_production_workflow_restores_saves_and_surfaces_media_cache(self) -> None:
        source = Path(".github/workflows/produce-resilient-v4.yml").read_text(encoding="utf-8")
        self.assertIn("Restore durable selected-media cache", source)
        self.assertIn(
            "ISCO_MEDIA_CACHE_DIR: ${{ runner.temp }}/isco-media-asset-cache", source
        )
        self.assertIn(
            "Prepare durable selected-media cache for cross-run save", source
        )
        self.assertIn("Save durable selected-media cache", source)
        self.assertIn(
            "media-asset-v1-${{ runner.os }}-${{ github.run_id }}", source
        )
        self.assertIn("engine/output/*/media-durable-cache-audit.json", source)


if __name__ == "__main__":
    unittest.main()
