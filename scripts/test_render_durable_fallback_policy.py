from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import render_durable_cache as render


_FFMPEG = {"first_line": "ffmpeg-test", "sha256": "f" * 64}


class RenderDurableFallbackPolicyTests(unittest.TestCase):
    @staticmethod
    def _env(root: Path) -> dict[str, str]:
        return {
            "ISCO_TTS_CACHE_PATH": str(root / "stage-cache"),
            "ISCO_APPROVED_BRIEF_SHA256": "a" * 64,
            "ISCO_ENGINE_SHA": "b" * 40,
        }

    def test_transient_cinematic_fallback_bytes_do_not_alias_later_enhanced_mux(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "run"
            out.mkdir(parents=True)
            uncarded = out / "uncarded.mp4"
            enhanced = out / "carded.mp4"
            narration = out / "narration.wav"
            uncarded.write_bytes(b"U" * 4096)
            enhanced.write_bytes(b"C" * 4096)
            narration.write_bytes(b"N" * 4096)

            def mux(*args, **kwargs):
                del args, kwargs

            with patch.dict(os.environ, self._env(root), clear=False), patch.object(
                render, "_ffmpeg_identity", return_value=_FFMPEG
            ):
                fallback_binding = render._final_binding(
                    uncarded,
                    narration,
                    mux,
                    None,
                    {"target_lufs": -16.0},
                )
                enhanced_binding = render._final_binding(
                    enhanced,
                    narration,
                    mux,
                    None,
                    {"target_lufs": -16.0},
                )

            self.assertIsNotNone(fallback_binding)
            self.assertIsNotNone(enhanced_binding)
            self.assertNotEqual(
                render._binding_hash(fallback_binding),
                render._binding_hash(enhanced_binding),
                "a later successful M10/CTA visual render must not reuse an older fallback final",
            )

    def test_no_audio_copy_mux_is_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "run"
            out.mkdir(parents=True)
            video = out / "picture.mp4"
            video.write_bytes(b"V" * 4096)
            calls = {"mux": 0}

            def mux(video_path, narration_path, output_path, music=None, **kwargs):
                del video_path, narration_path, music, kwargs
                calls["mux"] += 1
                Path(output_path).write_bytes(b"F" * 4096)
                return Path(output_path)

            with patch.dict(os.environ, self._env(root), clear=False), patch.object(
                render, "_ffmpeg_identity", return_value=_FFMPEG
            ):
                cache_root = render._shared_root()
                assert cache_root is not None
                cache_root.mkdir(parents=True, exist_ok=True)
                wrapped = render._wrap_mux(cache_root, mux)
                with render._final_candidate_scope() as pending:
                    wrapped(video, None, out / "final.mp4", music=None)
                    self.assertEqual(calls["mux"], 1)
                    self.assertEqual(pending, [])
                self.assertFalse((cache_root / "final").exists())

    def test_music_only_moment_mux_remains_cache_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "run"
            out.mkdir(parents=True)
            video = out / "picture.mp4"
            music = out / "music.wav"
            video.write_bytes(b"V" * 4096)
            music.write_bytes(b"M" * 4096)
            calls = {"mux": 0}

            def mux(video_path, narration_path, output_path, music=None, **kwargs):
                del video_path, narration_path, music, kwargs
                calls["mux"] += 1
                Path(output_path).write_bytes(b"F" * 8192)
                return Path(output_path)

            with patch.dict(os.environ, self._env(root), clear=False), patch.object(
                render, "_ffmpeg_identity", return_value=_FFMPEG
            ):
                cache_root = render._shared_root()
                assert cache_root is not None
                cache_root.mkdir(parents=True, exist_ok=True)
                wrapped = render._wrap_mux(cache_root, mux)
                with render._final_candidate_scope() as pending:
                    wrapped(video, None, out / "final.mp4", music=music, target_lufs=-18.0)
                    self.assertEqual(calls["mux"], 1)
                    self.assertEqual(len(pending), 1)
                    self.assertFalse(pending[0]["hit"])

    def test_render_owns_no_hard_cut_picture_concat_cache(self) -> None:
        self.assertNotIn("picture", render.MAX_ENTRIES_BY_KIND)
        self.assertFalse(hasattr(render, "_wrap_concat"))
        self.assertIn("m9-pair", render.MAX_ENTRIES_BY_KIND)


if __name__ == "__main__":
    unittest.main()
