"""Simple Mode V1: a run-mode flag on the existing pipeline (not a second engine).

Motivation (Runs 109-123): no run reached a finished video because subjective
LLM verdicts (tone audit, per-clip semantic review, opening review) decided whether
a video could exist at all. Simple Mode keeps every measurable safety check and the
proven script/voice/resume/edit path, and changes three things only:

1. Tone/naturalness verdicts are advisory unless they cite an objective problem
   (unverified religious quote, cultural dignity). Factuality stays blocking.
2. Stock visuals are ranked and chosen once; a beat with no usable stock gets the
   channel-colour background as the LAST resort. No per-clip semantic review gates
   the run and the 0.85 floor is not applied in this mode.
3. An honest report states what was NOT reviewed.

The flag is set by the immutable Telegram request, never mid-run. The default path is
untouched when the flag is absent.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from clean_v2.contracts import atomic_write_json

SIMPLE_MODE_ENV = "CLEAN_V2_SIMPLE_MODE"
FALLBACK_PROVIDER = "generated_local"
REPORT_FILE = "simple-mode-report.json"
_OBJECTIVE_TONE_FLAG_KEYS = (
    "unverified_religious_quote_flags",
    "cultural_dignity_flags",
)


def is_simple_mode() -> bool:
    return os.environ.get(SIMPLE_MODE_ENV, "").strip().lower() in {"1", "true", "yes"}


def tone_block_is_objective(report: Mapping[str, Any]) -> bool:
    """True when the tone verdict cites a measurable/safety problem, not taste."""
    return any(report.get(key) for key in _OBJECTIVE_TONE_FLAG_KEYS)


def advisory_tone_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the verdict as evidence, but do not let it stop a simple-mode run."""
    return {
        **dict(report),
        "status": "pass",
        "downgraded_from": "block",
        "downgrade_reason": "simple_mode_subjective_tone_verdict",
        "advisory_narrative_format_flags": list(report.get("narrative_format_flags") or []),
        "simple_mode_advisory": True,
    }


_INFRA_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}
_INFRA_REASON_MARKERS = ("http_408", "http_425", "http_429", "http_500", "http_502", "http_503", "http_504", "timeout", "timed out", "exhaust")


def is_known_infra_failure(exc: BaseException) -> bool:
    """True only for classified provider-capacity/availability failures.

    Anything else (programming errors, schema drift, contract violations) must keep
    propagating: an unknown exception is never turned into a warning.
    """
    import socket
    import urllib.error

    from clean_v2.mistral_executor import MistralExecutorWireFailure
    from clean_v2.providers import ProviderWireFailure

    if isinstance(exc, (TimeoutError, socket.timeout, urllib.error.URLError)):
        return True
    if isinstance(exc, ProviderWireFailure):
        if exc.http_status in _INFRA_HTTP_STATUS:
            return True
        return exc.http_status is None and any(m in exc.reason_code.lower() for m in _INFRA_REASON_MARKERS)
    if isinstance(exc, MistralExecutorWireFailure):
        return any(m in str(exc).lower() for m in _INFRA_REASON_MARKERS)
    return False


def unavailable_tone_report(exc: BaseException) -> dict[str, Any]:
    """Record an optional-audit outage honestly: it is NOT a passed audit."""
    return {
        "schema_version": 1,
        "source": "clean-v2-simple-mode-tone-audit-outage",
        "status": "pass",
        "audit_availability": "unavailable",
        "advisory_only": True,
        "tone_audit_reviewed": False,
        "infrastructure_failure": type(exc).__name__,
        "infrastructure_reason": str(exc)[:120],
        "decision_source": "simple_mode_optional_audit_unavailable",
    }


def tone_audit_state(output_dir: Path) -> str:
    """reviewed | advisory_downgraded | unavailable | unknown, from the saved report."""
    import json

    try:
        data = json.loads((Path(output_dir) / "tone-naturalness-audit.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unknown"
    if not isinstance(data, Mapping):
        return "unknown"
    if data.get("audit_availability") == "unavailable":
        return "unavailable"
    if data.get("simple_mode_advisory"):
        return "advisory_downgraded"
    return "reviewed"


def simple_visual_qa(
    *,
    output_dir: Path,
    plan: Mapping[str, Any],
    script: Mapping[str, Any],
    rights: list[dict[str, Any]],
    fmt: str,
    router: Any = None,
    visual_source: Any = None,
) -> dict[str, Any]:
    """No semantic review. Records exactly what was selected, never claims a match."""
    rows = [row for row in rights if isinstance(row, Mapping)]
    primary = [row for row in rows if not row.get("pacing_auxiliary")]
    extra = [row for row in rows if row.get("pacing_auxiliary")]

    def _is_bg(row: Mapping[str, Any]) -> bool:
        return row.get("provider") == FALLBACK_PROVIDER

    backgrounds = [row for row in primary if _is_bg(row)]
    extra_backgrounds = [row for row in extra if _is_bg(row)]
    report = {
        "status": "pass",
        "mode": "simple_unreviewed",
        "semantic_review": "not_performed",
        "clips": len(primary),
        "stock_clips": len(primary) - len(backgrounds),
        "fallback_backgrounds": len(backgrounds),
        "extra_pacing_clips": len(extra),
        "extra_pacing_stock": len(extra) - len(extra_backgrounds),
        "extra_pacing_backgrounds": len(extra_backgrounds),
        "final_media_mutated": False,
        "provider_calls_added": 0,
    }
    atomic_write_json(Path(output_dir) / "final-cut-visual-qa.json", report)
    return report


def simple_opening_director(**_kwargs: Any) -> dict[str, Any]:
    """The opening reviewer is an LLM gate; simple mode renders the selected clips as-is."""
    return {"status": "skipped_simple_mode", "slots": []}


def write_simple_report(output_dir: Path, *, fmt: str) -> dict[str, Any]:
    """Honest end-of-run report built only from recorded data."""
    output_dir = Path(output_dir)
    qa_path = output_dir / "final-cut-visual-qa.json"
    try:
        import json

        qa = json.loads(qa_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        qa = {}
    scenes = int(qa.get("clips") or 0)
    backgrounds = int(qa.get("fallback_backgrounds") or 0)
    stock = int(qa.get("stock_clips") or max(0, scenes - backgrounds))
    extra = int(qa.get("extra_pacing_clips") or 0)
    extra_bg = int(qa.get("extra_pacing_backgrounds") or 0)
    share = round(backgrounds / scenes, 3) if scenes else 0.0
    report_tone = tone_audit_state(output_dir)
    report = {
        "mode": "simple",
        "format": fmt,
        "scenes": scenes,
        "stock_clips_selected_by_ranking": stock,
        "fallback_backgrounds": backgrounds,
        "fallback_share": share,
        "extra_pacing_clips": extra,
        "extra_pacing_backgrounds": extra_bg,
        "semantic_visual_review": "not_performed",
        "tone_audit": tone_audit_state(output_dir),
        "technically_complete": True,
        "publish_decision": "owner",
        "statement": (
            f"{scenes} scenes: {stock} stock clips chosen by similarity ranking, "
            f"{backgrounds} channel backgrounds"
            + (f" (plus {extra} extra pacing clips, {extra_bg} of them backgrounds)" if extra else "")
            + ". Visual match was NOT reviewed; the owner decides whether to publish."
            + (" The tone audit was unavailable and was NOT passed." if report_tone == "unavailable" else "")
        ),
    }
    atomic_write_json(output_dir / REPORT_FILE, report)
    print(f"::notice title=simple-mode-report::{report['statement']}")
    return report


class SimpleModeSanityError(RuntimeError):
    """Measurable defect in the finished file (black or silent), never a taste verdict."""


def _ffmpeg_filter_text(path: Path, args: list[str]) -> str:
    import subprocess

    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), *args, "-f", "null", "-"],
        capture_output=True, text=True, timeout=600,
    )
    return proc.stderr or ""


def _detected_total(text: str, key: str) -> float:
    import re

    return sum(float(m) for m in re.findall(rf"{key}:\s*([0-9.]+)", text))


def final_video_sanity(path: Path, duration: float) -> dict[str, Any]:
    """Fail only on objective defects: mostly-black picture or mostly-silent audio."""
    black = _detected_total(
        _ffmpeg_filter_text(path, ["-vf", "blackdetect=d=0.5:pix_th=0.10", "-an"]), "black_duration"
    )
    silence = _detected_total(
        _ffmpeg_filter_text(path, ["-af", "silencedetect=n=-50dB:d=1.0", "-vn"]), "silence_duration"
    )
    report = {
        "duration_seconds": round(duration, 2),
        "black_seconds": round(black, 2),
        "silent_seconds": round(silence, 2),
        "status": "pass",
    }
    if duration > 0 and (black / duration > 0.6 or silence / duration > 0.6):
        report["status"] = "block"
        raise SimpleModeSanityError(
            f"simple_mode_final_file_mostly_black_or_silent black={black:.1f}s silent={silence:.1f}s of {duration:.1f}s"
        )
    return report


def simple_final_master_qc(output_dir: Path) -> dict[str, Any]:
    """Legacy technical QC plus black/silent detection; same signature as the gate."""
    from clean_v2.pipeline import _run_legacy_final_master_qc

    report = dict(_run_legacy_final_master_qc(output_dir))
    import json
    import subprocess

    final = Path(output_dir) / "final.mp4"
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(final)],
        capture_output=True, text=True, timeout=60,
    )
    duration = float(json.loads(probe.stdout or "{}").get("format", {}).get("duration") or 0)
    report["simple_mode_sanity"] = final_video_sanity(final, duration)
    return report
