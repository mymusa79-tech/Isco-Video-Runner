from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.cinematic_audio_mastering import master_narration_lite, mastering_report


_REPORT_NAME = "audio-mastering-report.json"


@contextmanager
def audio_mastering_scope() -> Iterator[None]:
    """Apply Audio Mastering Lite after narration concat and before final mux.

    The Engine's existing mux remains the only loudness-normalization/limiter authority.
    This scope only intercepts the canonical narration.wav concat output and restores the
    original function after the production call, including on failure.
    """
    original_concat_audio = orchestrator.concat_audio

    def concat_audio_bound(inputs, output):
        raw = original_concat_audio(inputs, output)
        raw_path = Path(raw)
        requested_output = Path(output)
        if requested_output.name != "narration.wav":
            return raw

        mastered = requested_output.with_name("narration-mastered.wav")
        result = master_narration_lite(raw_path, mastered)
        report = {
            **mastering_report(),
            "status": "applied",
            "production_stage": "post_concat_pre_sfx_pre_mux",
            "source": raw_path.name,
            "output": Path(result).name,
            "final_loudness_authority": "existing_mux_two_pass_loudnorm_and_limiter",
        }
        (requested_output.parent / _REPORT_NAME).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result

    orchestrator.concat_audio = concat_audio_bound
    try:
        yield
    finally:
        orchestrator.concat_audio = original_concat_audio


def install_audio_mastering_live_binding() -> None:
    current = orchestrator.produce
    if getattr(current, "_isco_audio_mastering_live_binding", False):
        return

    def wrapped(*args, **kwargs):
        with audio_mastering_scope():
            return current(*args, **kwargs)

    wrapped._isco_audio_mastering_live_binding = True
    wrapped._isco_audio_mastering_original = current
    orchestrator.produce = wrapped
