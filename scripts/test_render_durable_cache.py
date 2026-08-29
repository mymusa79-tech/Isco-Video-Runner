from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import render_durable_cache as render


class RenderDurableCacheTests(unittest.TestCase):
    @staticmethod
    def _env(root: Path) -> dict[str, str]:
        return {
            "ISCO_TTS_CACHE_PATH": str(root / "stage-cache"),
            "ISCO_APPROVED_BRIEF_SHA256": "a" * 64,
            "ISCO_ENGINE_SHA": "b" * 40,
        }

    @staticmethod
    def _write_context(root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "plan.json").write_text(
            json.dumps({"format": "film", "sections": [{"id": "s1", "narration": "x"}]}),
            encoding="utf-8",
        )
        (root / "visual-timeline.json").write_text(
            json.dumps({"duration_seconds": 12.0, "final_cut_visuals": []}),
            encoding="utf-8",
        )

    @staticmethod
    def _write_quality(root: Path, *, passed: bool) -> None:
        (root / "quality-final.json").write_text(
            json.dumps(
                {
                    "duration_ok": passed,
                    "audio_ok": passed,
                    "av_sync_ok": passed,
                    "video_streams": 1,
                }
            ),
            encoding="utf-8",
        )

    def test_final_is_not_persisted_until_current_qc_certifies_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "run1"
            self._write_context(out)
            video = out / "picture.mp4"
            narration = out / "narration.wav"
            music = out / "music.wav"
            video.write_bytes(b"V" * 4096)
            narration.write_bytes(b"N" * 4096)
            music.write_bytes(b"M" * 4096)
            calls = {"mux": 0}

            def mux(video_path, narration_path, output_path, music=None, **kwargs):
                del video_path, narration_path, music, kwargs
                calls["mux"] += 1
                output_path = Path(output_path)
                output_path.write_bytes(b"F" * 8192)
                for name in render._FINAL_SIDECARS:
                    (output_path.parent / name).write_text(
                        json.dumps({"name": name, "status": "applied"}), encoding="utf-8"
                    )
                return output_path

            with patch.dict(os.environ, self._env(root), clear=False):
                cache_root = render._shared_root()
                assert cache_root is not None
                cache_root.mkdir(parents=True, exist_ok=True)
                pending: list[dict] = []
                wrapped = render._wrap_mux(cache_root, mux, pending)
                final = wrapped(
                    video,
                    narration,
                    out / "final.mp4",
                    music=music,
                    target_lufs=-16.0,
                    music_gain=0.1,
                )
                self.assertEqual(final.read_bytes(), b"F" * 8192)
                self.assertEqual(calls["mux"], 1)
                self.assertEqual(len(pending), 1)
                entry = render._entry(cache_root, render._KIND_FINAL, pending[0]["fingerprint"])
                self.assertFalse(entry.exists(), "final must not enter durable storage before QC")

                self._write_quality(out, passed=True)
                render._reconcile_final_candidates(cache_root, pending)
                self.assertTrue((entry / "artifact.mp4").is_file())
                self.assertTrue((entry / "manifest.json").is_file())

    def test_final_hit_restores_mux_reports_skips_chain_and_is_evicted_on_current_qc_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run1 = root / "run1"
            run2 = root / "run2"
            self._write_context(run1)
            self._write_context(run2)
            calls = {"mux": 0}

            def mux(video_path, narration_path, output_path, music=None, **kwargs):
                del video_path, narration_path, music, kwargs
                calls["mux"] += 1
                output_path = Path(output_path)
                output_path.write_bytes(b"F" * 8192)
                for name in render._FINAL_SIDECARS:
                    (output_path.parent / name).write_text(
                        json.dumps({"name": name, "status": "applied"}), encoding="utf-8"
                    )
                return output_path

            with patch.dict(os.environ, self._env(root), clear=False):
                cache_root = render._shared_root()
                assert cache_root is not None
                cache_root.mkdir(parents=True, exist_ok=True)

                video1 = run1 / "picture.mp4"
                narration1 = run1 / "narration.wav"
                music1 = run1 / "music.wav"
                video1.write_bytes(b"V" * 4096)
                narration1.write_bytes(b"N" * 4096)
                music1.write_bytes(b"M" * 4096)
                pending1: list[dict] = []
                wrapped = render._wrap_mux(cache_root, mux, pending1)
                wrapped(video1, narration1, run1 / "final.mp4", music=music1, target_lufs=-16.0)
                self._write_quality(run1, passed=True)
                render._reconcile_final_candidates(cache_root, pending1)
                self.assertEqual(calls["mux"], 1)

                video2 = run2 / "picture.mp4"
                narration2 = run2 / "narration.wav"
                music2 = run2 / "music.wav"
                video2.write_bytes(b"V" * 4096)
                narration2.write_bytes(b"N" * 4096)
                music2.write_bytes(b"M" * 4096)
                for name in render._FINAL_SIDECARS:
                    self.assertFalse((run2 / name).exists())

                pending2: list[dict] = []
                wrapped2 = render._wrap_mux(cache_root, mux, pending2)
                final2 = wrapped2(video2, narration2, run2 / "final.mp4", music=music2, target_lufs=-16.0)
                self.assertEqual(final2.read_bytes(), b"F" * 8192)
                self.assertEqual(calls["mux"], 1, "durable hit must skip the expensive live mux chain")
                self.assertTrue(pending2[0]["hit"])
                for name in render._FINAL_SIDECARS:
                    self.assertTrue((run2 / name).is_file(), f"missing restored report: {name}")

                fingerprint = pending2[0]["fingerprint"]
                entry = render._entry(cache_root, render._KIND_FINAL, fingerprint)
                self.assertTrue(entry.exists())
                self._write_quality(run2, passed=False)
                render._reconcile_final_candidates(cache_root, pending2)
                self.assertFalse(entry.exists(), "a current QC failure must evict the cached final")

    def test_picture_hit_restores_m9_report_and_skips_live_assembly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run1 = root / "run1"
            run2 = root / "run2"
            run1.mkdir()
            run2.mkdir()
            timeline = json.dumps({"duration_seconds": 10.0, "final_cut_visuals": [{"shot_id": "a"}]})
            (run1 / "visual-timeline.json").write_text(timeline, encoding="utf-8")
            (run2 / "visual-timeline.json").write_text(timeline, encoding="utf-8")
            calls = {"concat": 0}

            def concat(inputs, output):
                del inputs
                calls["concat"] += 1
                output = Path(output)
                output.write_bytes(b"P" * 8192)
                (output.parent / "m9-transitions.json").write_text(
                    json.dumps({"status": "applied", "dissolve_count": 1}), encoding="utf-8"
                )
                return output

            with patch.dict(os.environ, self._env(root), clear=False):
                cache_root = render._shared_root()
                assert cache_root is not None
                cache_root.mkdir(parents=True, exist_ok=True)
                inputs1 = [run1 / "01.mp4", run1 / "02.mp4"]
                inputs2 = [run2 / "01.mp4", run2 / "02.mp4"]
                for path, payload in zip(inputs1, (b"A" * 4096, b"B" * 4096)):
                    path.write_bytes(payload)
                for path, payload in zip(inputs2, (b"A" * 4096, b"B" * 4096)):
                    path.write_bytes(payload)

                wrapped = render._wrap_concat(cache_root, concat)
                wrapped(inputs1, run1 / "picture.mp4")
                self.assertEqual(calls["concat"], 1)
                (run2 / "m9-transitions.json").unlink(missing_ok=True)
                wrapped(inputs2, run2 / "picture.mp4")
                self.assertEqual(calls["concat"], 1)
                self.assertTrue((run2 / "m9-transitions.json").is_file())

    def test_semantic_input_change_misses_final_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "run"
            self._write_context(out)
            video = out / "picture.mp4"
            narration = out / "narration.wav"
            video.write_bytes(b"V" * 4096)
            narration.write_bytes(b"N" * 4096)

            def mux(video_path, narration_path, output_path, music=None, **kwargs):
                del video_path, narration_path, music, kwargs
                Path(output_path).write_bytes(b"F" * 8192)
                return Path(output_path)

            with patch.dict(os.environ, self._env(root), clear=False):
                binding1 = render._final_binding(video, narration, mux, out / "final.mp4", None, {"target_lufs": -16.0})
                narration.write_bytes(b"Z" * 4096)
                binding2 = render._final_binding(video, narration, mux, out / "final.mp4", None, {"target_lufs": -16.0})
                self.assertNotEqual(render._binding_hash(binding1), render._binding_hash(binding2))


if __name__ == "__main__":
    unittest.main()
