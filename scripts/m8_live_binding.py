from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import isco_video_agent.cinematic_m7_runtime as m7_runtime
import isco_video_agent.media.ffmpeg as media_ffmpeg
import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.cinematic_m8_color_kernel import normalize_to_bt709_sdr, report_dict


@contextmanager
def m8_live_scope() -> Iterator[None]:
    """Normalize stock video to explicit BT.709 SDR before the existing creative grade."""
    original_media_prepare = media_ffmpeg.prepare_clip
    original_orchestrator_prepare = orchestrator.prepare_clip
    original_m7_prepare = m7_runtime.prepare_clip

    def prepare_clip_bound(src: Path, dest: Path, seconds: float, portrait: bool, fps: int = 30) -> Path:
        src = Path(src)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp_dir = dest.parent / ".m8"
        temp_dir.mkdir(parents=True, exist_ok=True)
        normalized = temp_dir / f"{dest.stem}-bt709-sdr.mp4"
        try:
            report = normalize_to_bt709_sdr(src, normalized)
            result = original_media_prepare(normalized, dest, seconds, portrait, fps)
            payload = {
                **report_dict(report),
                "status": "applied",
                "production_stage": "technical_normalization_before_creative_grade",
                "source": src.name,
                "final_clip": dest.name,
                "creative_grade_authority": "media.color.build_color_filter_after_m8",
            }
            dest.with_suffix(".m8.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return result
        finally:
            normalized.unlink(missing_ok=True)
            try:
                temp_dir.rmdir()
            except OSError:
                pass

    media_ffmpeg.prepare_clip = prepare_clip_bound
    orchestrator.prepare_clip = prepare_clip_bound
    m7_runtime.prepare_clip = prepare_clip_bound
    try:
        yield
    finally:
        media_ffmpeg.prepare_clip = original_media_prepare
        orchestrator.prepare_clip = original_orchestrator_prepare
        m7_runtime.prepare_clip = original_m7_prepare


def install_m8_live_binding() -> None:
    current = orchestrator.produce
    if getattr(current, "_isco_m8_live_binding", False):
        return

    def wrapped(*args, **kwargs):
        with m8_live_scope():
            return current(*args, **kwargs)

    wrapped._isco_m8_live_binding = True
    wrapped._isco_m8_original = current
    orchestrator.produce = wrapped
