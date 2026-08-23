from __future__ import annotations

import json
from pathlib import Path

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.cinematic_audio_mastering import master_narration_lite, mastering_report


_MARKER = "_isco_audio_mastering_runtime"
_REPORT_NAME = "audio-mastering.json"
_MASTERED_NAME = "narration-mastered.wav"


def _write_report(output_dir: Path, *, source: Path, mastered: Path) -> Path:
    report = mastering_report()
    report.update(
        {
            "status": "applied",
            "source_audio": source.name,
            "mastered_audio": mastered.name,
            "placement": "after_narration_concat_before_final_mux",
            "final_loudness_authority_unchanged": True,
        }
    )
    path = output_dir / _REPORT_NAME
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def install_audio_mastering_runtime() -> None:
    """Apply corrective narration mastering immediately before the existing mux.

    The Engine's existing mux remains the sole loudness-normalization/limiter authority.
    Mastering failure is authoritative for narrated production: no silent fallback to the
    unmastered narration is allowed after this runtime has been installed.
    """
    current = orchestrator.mux
    if getattr(current, _MARKER, False):
        return

    def mux_with_audio_mastering(video, audio, output, *args, **kwargs):
        source = Path(audio)
        output_path = Path(output)
        mastered = output_path.parent / _MASTERED_NAME
        master_narration_lite(source, mastered)
        _write_report(output_path.parent, source=source, mastered=mastered)
        return current(video, mastered, output, *args, **kwargs)

    setattr(mux_with_audio_mastering, _MARKER, True)
    setattr(mux_with_audio_mastering, "_isco_audio_mastering_original", current)
    orchestrator.mux = mux_with_audio_mastering
    print(
        "Audio Mastering Lite runtime installed: narration concat -> corrective mastering -> "
        "existing mux loudnorm/limiter"
    )
