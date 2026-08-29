from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.visual_selection as visual_selection

from scripts import media_durable_cache as durable
from scripts import media_trust_boundary_v2 as trust


class MediaDurableCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        durable.reset_media_durable_cache_for_tests()
        trust._records_by_url.clear()
        trust._records_by_path.clear()
        durable._LOCAL_REVALIDATED_RAW.clear()

    def tearDown(self) -> None:
        durable.reset_media_durable_cache_for_tests()
        trust._records_by_url.clear()
        trust._records_by_path.clear()
        durable._LOCAL_REVALIDATED_RAW.clear()

    def _env(self, root: Path) -> dict[str, str]:
        return {
            "ISCO_MEDIA_CACHE_PATH": str(root / "cache"),
            "ISCO_APPROVED_BRIEF_SHA256": "a" * 64,
            "ISCO_ENGINE_SHA": "b" * 40,
            "GEMINI_CONTENT_MODEL": "gemini-3.7-flash",
        }

    @staticmethod
    def _candidate(url: str, asset_id: int = 101) -> dict:
        return {
            "id": asset_id,
            "duration": 30,
            "video_files": [
                {"id": 1, "quality": "hd", "width": 1920, "height": 1080, "link": url}
            ],
            "url": f"https://www.pexels.com/video/{asset_id}/",
        }

    @staticmethod
    def _cloud_pass() -> dict:
        return {
            "status": "pass",
            "review_origin": "cloud_visual_qa",
            "vision_review_performed": True,
            "relevance": 9.0,
            "visual_quality": 9.0,
            "reason": "safe and relevant",
        }

    def test_cloud_verdict_and_exact_raw_bytes_resume_without_provider_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            url = "https://videos.pexels.com/video-files/101/101-hd.mp4"
            candidate = self._candidate(url)
            calls = {"download": 0, "audit": 0}
            quarantine = root / "quarantine"
            quarantine.mkdir()

            def original_download(provider: str, source_url: str, dest: Path) -> Path:
                calls["download"] += 1
                stored = quarantine / f"source-{calls['download']}.mp4"
                stored.write_bytes(b"R" * 4096)
                record = trust.TrustedMediaRecord(
                    provider=provider,
                    source_url=source_url,
                    final_url=source_url,
                    sha256=durable._sha256_file(stored),
                    byte_length=stored.stat().st_size,
                    quarantine_path=stored,
                )
                trust._records_by_url[(provider, source_url)] = record
                return trust._materialize_verified(record, Path(dest))

            def audit_fn(*, provider, candidate, narration_context, intended_visual):
                calls["audit"] += 1
                source_url = candidate["video_files"][0]["link"]
                trust.trusted_download(provider, source_url, root / "review" / "candidate.mp4")
                return self._cloud_pass()

            with patch.dict(os.environ, self._env(root), clear=False), \
                    patch.object(trust, "trusted_download", original_download), \
                    patch.object(trust, "_inspect_exact_review_source", return_value=None):
                durable.install_media_durable_cache()
                first_cache = visual_selection.VisualCandidateCache(excluded_assets={})
                first = visual_selection.review_candidates(
                    [("pexels", candidate)],
                    narration_context="السياق نفسه",
                    intended_visual="شخص يبدأ من جديد",
                    audit_fn=audit_fn,
                    cache=first_cache,
                    max_candidates=1,
                    max_total_candidates=1,
                )
                self.assertEqual(first.status, "selected")
                self.assertFalse(first.chosen.from_cache)
                self.assertEqual(calls, {"download": 1, "audit": 1})

                # Simulate the next process/run: process-local trust and selector state
                # disappear, while the durable cache directory survives.
                trust._records_by_url.clear()
                trust._records_by_path.clear()
                durable._LOCAL_REVALIDATED_RAW.clear()
                second_cache = visual_selection.VisualCandidateCache(excluded_assets={})

                def must_not_audit(**_kwargs):
                    raise AssertionError("cloud Vision should not be called for a valid durable verdict")

                second = visual_selection.review_candidates(
                    [("pexels", candidate)],
                    narration_context="السياق نفسه",
                    intended_visual="شخص يبدأ من جديد",
                    audit_fn=must_not_audit,
                    cache=second_cache,
                    max_candidates=1,
                    max_total_candidates=1,
                )
                self.assertEqual(second.status, "selected")
                self.assertTrue(second.chosen.from_cache)

                rendered_raw = trust.trusted_download("pexels", url, root / "run2" / "raw.mp4")
                self.assertEqual(rendered_raw.read_bytes(), b"R" * 4096)
                self.assertEqual(calls, {"download": 1, "audit": 1})
                self.assertTrue(durable.prepare_cache_for_persistence(root / "cache"))

    def test_changed_context_never_reuses_cloud_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            url = "https://videos.pexels.com/video-files/102/102-hd.mp4"
            candidate = self._candidate(url, 102)
            calls = {"audit": 0}
            quarantine = root / "quarantine"
            quarantine.mkdir()

            def original_download(provider: str, source_url: str, dest: Path) -> Path:
                stored = quarantine / "source.mp4"
                stored.write_bytes(b"S" * 4096)
                record = trust.TrustedMediaRecord(
                    provider=provider,
                    source_url=source_url,
                    final_url=source_url,
                    sha256=durable._sha256_file(stored),
                    byte_length=stored.stat().st_size,
                    quarantine_path=stored,
                )
                trust._records_by_url[(provider, source_url)] = record
                return trust._materialize_verified(record, Path(dest))

            def audit_fn(*, provider, candidate, narration_context, intended_visual):
                calls["audit"] += 1
                trust.trusted_download(provider, url, root / f"review-{calls['audit']}.mp4")
                return self._cloud_pass()

            with patch.dict(os.environ, self._env(root), clear=False), \
                    patch.object(trust, "trusted_download", original_download), \
                    patch.object(trust, "_inspect_exact_review_source", return_value=None):
                durable.install_media_durable_cache()
                visual_selection.review_candidates(
                    [("pexels", candidate)],
                    narration_context="السياق الأول",
                    intended_visual="فكرة أولى",
                    audit_fn=audit_fn,
                    cache=visual_selection.VisualCandidateCache(excluded_assets={}),
                    max_candidates=1,
                    max_total_candidates=1,
                )
                trust._records_by_url.clear()
                trust._records_by_path.clear()
                durable._LOCAL_REVALIDATED_RAW.clear()
                visual_selection.review_candidates(
                    [("pexels", candidate)],
                    narration_context="سياق مختلف",
                    intended_visual="فكرة أولى",
                    audit_fn=audit_fn,
                    cache=visual_selection.VisualCandidateCache(excluded_assets={}),
                    max_candidates=1,
                    max_total_candidates=1,
                )
            self.assertEqual(calls["audit"], 2)

    def test_tampered_raw_bytes_force_normal_review_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            url = "https://videos.pexels.com/video-files/103/103-hd.mp4"
            candidate = self._candidate(url, 103)
            calls = {"download": 0, "audit": 0}
            quarantine = root / "quarantine"
            quarantine.mkdir()

            def original_download(provider: str, source_url: str, dest: Path) -> Path:
                calls["download"] += 1
                stored = quarantine / f"source-{calls['download']}.mp4"
                stored.write_bytes(bytes([70 + calls["download"]]) * 4096)
                record = trust.TrustedMediaRecord(
                    provider=provider,
                    source_url=source_url,
                    final_url=source_url,
                    sha256=durable._sha256_file(stored),
                    byte_length=stored.stat().st_size,
                    quarantine_path=stored,
                )
                trust._records_by_url[(provider, source_url)] = record
                return trust._materialize_verified(record, Path(dest))

            def audit_fn(*, provider, candidate, narration_context, intended_visual):
                calls["audit"] += 1
                trust.trusted_download(provider, url, root / f"review-{calls['audit']}.mp4")
                return self._cloud_pass()

            with patch.dict(os.environ, self._env(root), clear=False), \
                    patch.object(trust, "trusted_download", original_download), \
                    patch.object(trust, "_inspect_exact_review_source", return_value=None):
                durable.install_media_durable_cache()
                visual_selection.review_candidates(
                    [("pexels", candidate)],
                    narration_context="سياق",
                    intended_visual="فكرة",
                    audit_fn=audit_fn,
                    cache=visual_selection.VisualCandidateCache(excluded_assets={}),
                    max_candidates=1,
                    max_total_candidates=1,
                )
                raw_entries = list((root / "cache" / "raw").iterdir())
                self.assertEqual(len(raw_entries), 1)
                raw_file = next(path for path in raw_entries[0].iterdir() if path.name.startswith("source"))
                raw_file.write_bytes(b"X" + raw_file.read_bytes()[1:])

                trust._records_by_url.clear()
                trust._records_by_path.clear()
                durable._LOCAL_REVALIDATED_RAW.clear()
                visual_selection.review_candidates(
                    [("pexels", candidate)],
                    narration_context="سياق",
                    intended_visual="فكرة",
                    audit_fn=audit_fn,
                    cache=visual_selection.VisualCandidateCache(excluded_assets={}),
                    max_candidates=1,
                    max_total_candidates=1,
                )
            self.assertEqual(calls["audit"], 2)
            self.assertEqual(calls["download"], 2)

    def test_prepared_clip_reuses_exact_trusted_source_without_ffmpeg_rerender(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            url = "https://videos.pexels.com/video-files/104/104-hd.mp4"
            calls = {"download": 0, "prepare": 0}
            quarantine = root / "quarantine"
            quarantine.mkdir()

            def original_download(provider: str, source_url: str, dest: Path) -> Path:
                calls["download"] += 1
                stored = quarantine / f"source-{calls['download']}.mp4"
                stored.write_bytes(b"M" * 4096)
                record = trust.TrustedMediaRecord(
                    provider=provider,
                    source_url=source_url,
                    final_url=source_url,
                    sha256=durable._sha256_file(stored),
                    byte_length=stored.stat().st_size,
                    quarantine_path=stored,
                )
                trust._records_by_url[(provider, source_url)] = record
                return trust._materialize_verified(record, Path(dest))

            def original_prepare(src: Path, dest: Path, seconds: float, portrait: bool, fps: int = 30) -> Path:
                calls["prepare"] += 1
                dest = Path(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"P" * 4096)
                return dest

            with patch.dict(os.environ, self._env(root), clear=False), \
                    patch.object(trust, "trusted_download", original_download), \
                    patch.object(orchestrator, "prepare_clip", original_prepare), \
                    patch.object(orchestrator, "duration", return_value=12.0):
                durable.install_media_durable_cache()
                raw1 = trust.trusted_download("pexels", url, root / "run1" / "raw.mp4")
                clip1 = orchestrator.prepare_clip(raw1, root / "run1" / "prepared.mp4", 12.0, False, fps=30)
                self.assertEqual(clip1.read_bytes(), b"P" * 4096)

                trust._records_by_url.clear()
                trust._records_by_path.clear()
                raw2 = trust.trusted_download("pexels", url, root / "run2" / "raw.mp4")
                clip2 = orchestrator.prepare_clip(raw2, root / "run2" / "prepared.mp4", 12.0, False, fps=30)

            self.assertEqual(clip2.read_bytes(), b"P" * 4096)
            self.assertEqual(calls["download"], 1)
            self.assertEqual(calls["prepare"], 1)


if __name__ == "__main__":
    unittest.main()
