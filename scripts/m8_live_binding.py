from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import isco_video_agent.cinematic_m7_runtime as m7_runtime
import isco_video_agent.media.ffmpeg as media_ffmpeg
import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.cinematic_m8_color_kernel import normalize_to_bt709_sdr, report_dict
from scripts.media_durable_asset_cache import prepare_trusted_clip_with_cache


@contextmanager
def m8_live_scope() -> Iterator[None]:
    """Normalize stock video to explicit BT.709 SDR before the existing creative grade."""
    original_media_prepare = media_ffmpeg.prepare_clip
    original_orchestrator_prepare = orchestrator.prepare_clip
    original_m7_prepare = m7_runtime.prepare_clip

    def prepare_clip_bound(src: Path, dest: Path, seconds: float, portrait: bool, fps: int = 30) -> Path:
        src = Path(src)
        dest = Path(dest)

        def render_full_m8_pipeline(
            pipeline_src: Path,
            pipeline_dest: Path,
            pipeline_seconds: float,
            pipeline_portrait: bool,
            fps: int = 30,
        ) -> Path:
            pipeline_src = Path(pipeline_src)
            pipeline_dest = Path(pipeline_dest)
            pipeline_dest.parent.mkdir(parents=True, exist_ok=True)
            temp_dir = pipeline_dest.parent / ".m8"
            temp_dir.mkdir(parents=True, exist_ok=True)
            normalized = temp_dir / f"{pipeline_dest.stem}-bt709-sdr.mp4"
            try:
                report = normalize_to_bt709_sdr(pipeline_src, normalized)
                result = original_media_prepare(
                    normalized,
                    pipeline_dest,
                    pipeline_seconds,
                    pipeline_portrait,
                    fps,
                )
                payload = {
                    **report_dict(report),
                    "status": "applied",
                    "production_stage": "technical_normalization_before_creative_grade",
                    "source": pipeline_src.name,
                    "final_clip": pipeline_dest.name,
                    "creative_grade_authority": "media.color.build_color_filter_after_m8",
                }
                pipeline_dest.with_suffix(".m8.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                return result
            finally:
                normalized.unlink(missing_ok=True)
                try:
                    temp_dir.rmdir()
                except OSError:
                    pass

        # Durable media is a resume optimization around the COMPLETE M8 path. Putting
        # the cache beneath this scope would silently miss production because M8 replaces
        # orchestrator.prepare_clip dynamically. A hit restores both the final prepared
        # clip and its M8 evidence; Media Trust remains the prerequisite for eligibility.
        if str(os.environ.get("ISCO_MEDIA_CACHE_DIR") or "").strip():
            return prepare_trusted_clip_with_cache(
                render_full_m8_pipeline,
                src,
                dest,
                seconds,
                portrait,
                fps=fps,
            )
        return render_full_m8_pipeline(src, dest, seconds, portrait, fps=fps)

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
