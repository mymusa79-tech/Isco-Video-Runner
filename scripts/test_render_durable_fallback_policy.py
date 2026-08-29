from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import render_durable_cache as render


class RenderDurableFallbackPolicyTests(unittest.TestCase):
    @staticmethod
    def _env(root: Path) -> dict[str, str]:
        return {
            "ISCO_TTS_CACHE_PATH": str(root / "stage-cache"),
            "ISCO_APPROVED_BRIEF_SHA256": "a" * 64,
            "ISCO_ENGINE_SHA": "b" * 40,
        }

    @staticmethod
    def _context(root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "plan.json").write_text(json.dumps({"format": "film", "sections": []}), encoding="utf-8")
        (root / "visual-timeline.json").write_text(
            json.dumps({"duration_seconds": 12.0, "final_cut_visuals": []}), encoding="utf-8"
        )
        (root / "quality-final.json").write_text(
            json.dumps({"duration_ok": True, "audio_ok": True, "av_sync_ok": True, "video_streams": 1}),
            encoding="utf-8",
        )

    def test_qc_pass_does_not_promote_transient_m10_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "run"
            self._context(out)
            video = out / "picture.mp4"
            narration = out / "narration.wav"
            video.write_bytes(b"V" * 4096)
            narration.write_bytes(b"N" * 4096)

            def mux(video_path, narration_path, output_path, music=None, **kwargs):
                del video_path, narration_path, music, kwargs
                output_path = Path(output_path)
                output_path.write_bytes(b"F" * 8192)
                (out / "m10-cards.json").write_text(
                    json.dumps({"status": "render_error_fallback_to_uncarded_video"}),
                    encoding="utf-8",
                )
                return output_path

            with patch.dict(os.environ, self._env(root), clear=False):
                cache_root = render._shared_root()
                assert cache_root is not None
                cache_root.mkdir(parents=True, exist_ok=True)
                pending: list[dict] = []
                wrapped = render._wrap_mux(cache_root, mux, pending)
                wrapped(video, narration, out / "final.mp4", target_lufs=-16.0)
                render._reconcile_final_candidates(cache_root, pending)
                entry = render._entry(cache_root, render._KIND_FINAL, pending[0]["fingerprint"])
                self.assertFalse(entry.exists())

    def test_no_audio_copy_mux_is_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "run"
            self._context(out)
            video = out / "picture.mp4"
            video.write_bytes(b"V" * 4096)
            calls = {"mux": 0}

            def mux(video_path, narration_path, output_path, music=None, **kwargs):
                del video_path, narration_path, music, kwargs
                calls["mux"] += 1
                Path(output_path).write_bytes(b"F" * 4096)
                return Path(output_path)

            with patch.dict(os.environ, self._env(root), clear=False):
                cache_root = render._shared_root()
                assert cache_root is not None
                pending: list[dict] = []
                wrapped = render._wrap_mux(cache_root, mux, pending)
                wrapped(video, None, out / "final.mp4", music=None)
                self.assertEqual(calls["mux"], 1)
                self.assertEqual(pending, [])

    def test_hard_cut_only_picture_is_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "run"
            out.mkdir(parents=True)
            (out / "visual-timeline.json").write_text(
                json.dumps({"duration_seconds": 10.0, "final_cut_visuals": [{"shot_id": "a"}]}),
                encoding="utf-8",
            )
            inputs = [out / "01.mp4", out / "02.mp4"]
            inputs[0].write_bytes(b"A" * 4096)
            inputs[1].write_bytes(b"B" * 4096)

            def concat(paths, output):
                del paths
                output = Path(output)
                output.write_bytes(b"P" * 8192)
                (out / "m9-transitions.json").write_text(
                    json.dumps({"status": "hard_cut_only", "dissolve_count": 0}),
                    encoding="utf-8",
                )
                return output

            with patch.dict(os.environ, self._env(root), clear=False):
                cache_root = render._shared_root()
                assert cache_root is not None
                cache_root.mkdir(parents=True, exist_ok=True)
                wrapped = render._wrap_concat(cache_root, concat)
                wrapped(inputs, out / "picture.mp4")
                self.assertFalse((cache_root / render._KIND_PICTURE).exists())


if __name__ == "__main__":
    unittest.main()
