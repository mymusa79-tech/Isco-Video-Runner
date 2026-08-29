from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import render_durable_cache as render


_FFMPEG_A = {"first_line": "ffmpeg-test-a", "sha256": "f" * 64}
_FFMPEG_B = {"first_line": "ffmpeg-test-b", "sha256": "e" * 64}
_FONT = {"filename": "NotoSansArabic.ttf", "sha256": "d" * 64}


class RenderDurableCacheTests(unittest.TestCase):
    @staticmethod
    def _env(root: Path) -> dict[str, str]:
        return {
            "ISCO_TTS_CACHE_PATH": str(root / "stage-cache"),
            "ISCO_APPROVED_BRIEF_SHA256": "a" * 64,
            "ISCO_ENGINE_SHA": "b" * 40,
        }

    @staticmethod
    def _write_quality(root: Path, *, passed: bool = True, fmt: str = "film") -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "quality-final.json").write_text(
            json.dumps(
                {
                    "duration_ok": passed,
                    "audio_ok": passed,
                    "av_sync_ok": passed,
                    "video_streams": 1,
                    "audio_streams": 0 if fmt == "moment" else 1,
                    "format": fmt,
                }
            ),
            encoding="utf-8",
        )

    def test_final_is_promoted_only_after_fresh_engine_qc_and_then_hits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run1 = root / "run1"
            run2 = root / "run2"
            run1.mkdir()
            run2.mkdir()
            calls = {"mux": 0}

            def mux(video_path, narration_path, output_path, music=None, **kwargs):
                del video_path, narration_path, music, kwargs
                calls["mux"] += 1
                output_path = Path(output_path)
                output_path.write_bytes(b"F" * 8192)
                return output_path

            with patch.dict(os.environ, self._env(root), clear=False), patch.object(
                render, "_ffmpeg_identity", return_value=_FFMPEG_A
            ):
                cache_root = render._shared_root()
                assert cache_root is not None
                cache_root.mkdir(parents=True, exist_ok=True)

                video1 = run1 / "picture.mp4"
                narration1 = run1 / "narration.wav"
                music1 = run1 / "music.wav"
                video1.write_bytes(b"V" * 4096)
                narration1.write_bytes(b"N" * 4096)
                music1.write_bytes(b"M" * 4096)
                self._write_quality(run1, passed=True)  # stale document must be removed at mux entry.

                wrapped = render._wrap_mux(cache_root, mux)
                with render._final_candidate_scope() as pending1:
                    final1 = wrapped(
                        video1,
                        narration1,
                        run1 / "final.mp4",
                        music=music1,
                        target_lufs=-16.0,
                        music_gain=0.1,
                    )
                    self.assertFalse((run1 / "quality-final.json").exists())
                    self.assertEqual(calls["mux"], 1)
                    self.assertEqual(len(pending1), 1)
                    entry = render._entry(cache_root, "final", pending1[0]["fingerprint"])
                    self.assertFalse(entry.exists(), "final must not persist before current Engine QC")
                    self._write_quality(run1, passed=True)
                    render._reconcile_final_candidates(cache_root, pending1)
                    self.assertTrue((entry / "artifact.mp4").is_file())
                    self.assertEqual(final1.read_bytes(), b"F" * 8192)

                video2 = run2 / "picture.mp4"
                narration2 = run2 / "narration.wav"
                music2 = run2 / "music.wav"
                video2.write_bytes(b"V" * 4096)
                narration2.write_bytes(b"N" * 4096)
                music2.write_bytes(b"M" * 4096)
                with render._final_candidate_scope() as pending2:
                    final2 = wrapped(
                        video2,
                        narration2,
                        run2 / "final.mp4",
                        music=music2,
                        target_lufs=-16.0,
                        music_gain=0.1,
                    )
                    self.assertEqual(calls["mux"], 1, "inner Engine mux must be skipped on exact hit")
                    self.assertTrue(pending2[0]["hit"])
                    self.assertEqual(final2.read_bytes(), b"F" * 8192)
                    self._write_quality(run2, passed=True)
                    render._reconcile_final_candidates(cache_root, pending2)
                    self.assertTrue(entry.exists())

    def test_current_qc_rejection_evicts_a_restored_final(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "run"
            out.mkdir()
            video = out / "picture.mp4"
            narration = out / "narration.wav"
            video.write_bytes(b"V" * 4096)
            narration.write_bytes(b"N" * 4096)

            def mux(video_path, narration_path, output_path, music=None, **kwargs):
                del video_path, narration_path, music, kwargs
                Path(output_path).write_bytes(b"F" * 8192)
                return Path(output_path)

            with patch.dict(os.environ, self._env(root), clear=False), patch.object(
                render, "_ffmpeg_identity", return_value=_FFMPEG_A
            ):
                cache_root = render._shared_root()
                assert cache_root is not None
                binding = render._final_binding(video, narration, mux, None, {"target_lufs": -16.0})
                assert binding is not None
                fingerprint = render._binding_hash(binding)
                cached_source = root / "cached.mp4"
                cached_source.write_bytes(b"F" * 8192)
                render._persist_entry(cache_root, "final", fingerprint, binding, cached_source)

                wrapped = render._wrap_mux(cache_root, mux)
                with render._final_candidate_scope() as pending:
                    wrapped(video, narration, out / "final.mp4", target_lufs=-16.0)
                    self.assertTrue(pending[0]["hit"])
                    self._write_quality(out, passed=False)
                    render._reconcile_final_candidates(cache_root, pending)
                self.assertFalse(render._entry(cache_root, "final", fingerprint).exists())

    def test_missing_or_symlink_audio_cannot_alias_no_audio_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "run"
            out.mkdir()
            video = out / "picture.mp4"
            video.write_bytes(b"V" * 4096)
            missing = out / "missing.wav"
            target = out / "target.wav"
            target.write_bytes(b"N" * 4096)
            symlink = out / "linked.wav"
            try:
                symlink.symlink_to(target)
            except OSError:
                symlink = missing

            def mux(*args, **kwargs):
                del args, kwargs
                return out / "final.mp4"

            with patch.dict(os.environ, self._env(root), clear=False), patch.object(
                render, "_ffmpeg_identity", return_value=_FFMPEG_A
            ):
                self.assertIsNone(render._final_binding(video, missing, mux, None, {}))
                self.assertIsNone(render._final_binding(video, symlink, mux, None, {}))

    def test_all_mux_kwargs_and_ffmpeg_identity_are_semantic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "run"
            out.mkdir()
            video = out / "picture.mp4"
            narration = out / "narration.wav"
            video.write_bytes(b"V" * 4096)
            narration.write_bytes(b"N" * 4096)

            def mux(*args, **kwargs):
                del args, kwargs

            with patch.dict(os.environ, self._env(root), clear=False), patch.object(
                render, "_ffmpeg_identity", return_value=_FFMPEG_A
            ):
                base = render._final_binding(video, narration, mux, None, {"target_lufs": -16.0})
                future_kw = render._final_binding(
                    video,
                    narration,
                    mux,
                    None,
                    {"target_lufs": -16.0, "future_semantic_knob": 1},
                )
                self.assertNotEqual(render._binding_hash(base), render._binding_hash(future_kw))
            with patch.object(render, "_ffmpeg_identity", return_value=_FFMPEG_B):
                changed_runtime = render._final_binding(video, narration, mux, None, {"target_lufs": -16.0})
            self.assertNotEqual(render._binding_hash(base), render._binding_hash(changed_runtime))

    def test_m9_pair_cache_skips_only_expensive_xfade_pair_not_m9_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run1 = root / "run1"
            run2 = root / "run2"
            run1.mkdir()
            run2.mkdir()
            left1, right1 = run1 / "left.mp4", run1 / "right.mp4"
            left2, right2 = run2 / "left.mp4", run2 / "right.mp4"
            for path, payload in ((left1, b"L" * 4096), (left2, b"L" * 4096), (right1, b"R" * 4096), (right2, b"R" * 4096)):
                path.write_bytes(payload)
            calls = {"pair": 0}

            def pair(left, right, dest, *, dissolve_seconds=0.36):
                del left, right, dissolve_seconds
                calls["pair"] += 1
                Path(dest).write_bytes(b"P" * 8192)
                return Path(dest)

            with patch.dict(os.environ, self._env(root), clear=False), patch.object(
                render, "_ffmpeg_identity", return_value=_FFMPEG_A
            ):
                cache_root = render._shared_root()
                assert cache_root is not None
                wrapped = render._wrap_m9_pair(cache_root, pair)
                wrapped(left1, right1, run1 / "pair.mp4", dissolve_seconds=0.36)
                wrapped(left2, right2, run2 / "pair.mp4", dissolve_seconds=0.36)
                self.assertEqual(calls["pair"], 1)

    def test_subtitle_burn_cache_binds_font_and_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run1 = root / "run1"
            run2 = root / "run2"
            run1.mkdir()
            run2.mkdir()
            calls = {"burn": 0}

            def burn(video, srt, output, *, portrait=False):
                del video, srt, portrait
                calls["burn"] += 1
                Path(output).write_bytes(b"B" * 8192)
                return Path(output)

            with patch.dict(os.environ, self._env(root), clear=False), patch.object(
                render, "_ffmpeg_identity", return_value=_FFMPEG_A
            ), patch.object(render, "_font_identity", return_value=_FONT):
                cache_root = render._shared_root()
                assert cache_root is not None
                wrapped = render._wrap_burn(cache_root, burn)
                for run in (run1, run2):
                    (run / "picture.mp4").write_bytes(b"V" * 4096)
                    (run / "selective.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nمرحبا\n", encoding="utf-8")
                    wrapped(run / "picture.mp4", run / "selective.srt", run / "picture-text.mp4")
                self.assertEqual(calls["burn"], 1)


if __name__ == "__main__":
    unittest.main()
