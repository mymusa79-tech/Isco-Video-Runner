from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import m8_live_binding as binding


class M8LiveBindingTests(unittest.TestCase):
    def test_normalization_precedes_existing_creative_grade_and_writes_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "source.mp4"
            dest = root / "prepared.mp4"
            src.write_bytes(b"source")
            calls: list[tuple[str, Path]] = []

            def fake_normalize(source: Path, normalized: Path):
                calls.append(("normalize", Path(source)))
                Path(normalized).write_bytes(b"normalized")
                return SimpleNamespace(classification="BT709_SDR_EXPLICIT")

            def fake_prepare(source: Path, output: Path, seconds: float, portrait: bool, fps: int = 30):
                calls.append(("creative_grade", Path(source)))
                self.assertTrue(Path(source).name.endswith("-bt709-sdr.mp4"))
                Path(output).write_bytes(b"prepared")
                return Path(output)

            with patch.object(binding, "normalize_to_bt709_sdr", side_effect=fake_normalize), patch.object(
                binding, "report_dict", return_value={"classification": "BT709_SDR_EXPLICIT", "creative_grade_applied": False}
            ), patch.object(binding.media_ffmpeg, "prepare_clip", fake_prepare), patch.object(
                binding.orchestrator, "prepare_clip", fake_prepare
            ), patch.object(binding.m7_runtime, "prepare_clip", fake_prepare):
                with binding.m8_live_scope():
                    result = binding.orchestrator.prepare_clip(src, dest, 8.0, False, 30)

            self.assertEqual(result, dest)
            self.assertEqual([name for name, _ in calls], ["normalize", "creative_grade"])
            evidence = dest.with_suffix(".m8.json").read_text(encoding="utf-8")
            self.assertIn('"production_stage": "technical_normalization_before_creative_grade"', evidence)
            self.assertIn('"creative_grade_authority": "media.color.build_color_filter_after_m8"', evidence)
            self.assertFalse((dest.parent / ".m8").exists())

    def test_same_binding_covers_media_orchestrator_and_live_m7_prepare_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "source.mp4"
            src.write_bytes(b"source")
            normalized_sources: list[Path] = []

            def fake_normalize(_source: Path, normalized: Path):
                Path(normalized).write_bytes(b"normalized")
                return object()

            def original_prepare(source: Path, output: Path, seconds: float, portrait: bool, fps: int = 30):
                normalized_sources.append(Path(source))
                Path(output).write_bytes(b"ok")
                return Path(output)

            with patch.object(binding, "normalize_to_bt709_sdr", side_effect=fake_normalize), patch.object(
                binding, "report_dict", return_value={}
            ), patch.object(binding.media_ffmpeg, "prepare_clip", original_prepare), patch.object(
                binding.orchestrator, "prepare_clip", original_prepare
            ), patch.object(binding.m7_runtime, "prepare_clip", original_prepare):
                with binding.m8_live_scope():
                    binding.media_ffmpeg.prepare_clip(src, root / "a.mp4", 4.0, False)
                    binding.orchestrator.prepare_clip(src, root / "b.mp4", 4.0, False)
                    binding.m7_runtime.prepare_clip(src, root / "c.mp4", 4.0, False)

            self.assertEqual(len(normalized_sources), 3)
            self.assertTrue(all(path.name.endswith("-bt709-sdr.mp4") for path in normalized_sources))

    def test_normalization_failure_is_fail_closed_and_restores_hooks(self) -> None:
        original_media = binding.media_ffmpeg.prepare_clip
        original_orchestrator = binding.orchestrator.prepare_clip
        original_m7 = binding.m7_runtime.prepare_clip
        with patch.object(binding, "normalize_to_bt709_sdr", side_effect=RuntimeError("ambiguous color metadata")):
            with self.assertRaisesRegex(RuntimeError, "ambiguous color metadata"):
                with binding.m8_live_scope():
                    binding.orchestrator.prepare_clip(Path("source.mp4"), Path("prepared.mp4"), 3.0, False)
        self.assertIs(binding.media_ffmpeg.prepare_clip, original_media)
        self.assertIs(binding.orchestrator.prepare_clip, original_orchestrator)
        self.assertIs(binding.m7_runtime.prepare_clip, original_m7)

    def test_install_is_idempotent(self) -> None:
        def base_produce(*args, **kwargs):
            return "ok"

        with patch.object(binding.orchestrator, "produce", base_produce):
            binding.install_m8_live_binding()
            first = binding.orchestrator.produce
            binding.install_m8_live_binding()
            second = binding.orchestrator.produce
            self.assertIs(first, second)
            self.assertTrue(getattr(first, "_isco_m8_live_binding", False))
            self.assertIs(getattr(first, "_isco_m8_original", None), base_produce)


if __name__ == "__main__":
    unittest.main()
