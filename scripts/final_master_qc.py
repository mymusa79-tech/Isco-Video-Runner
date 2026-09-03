from __future__ import annotations

import hashlib
import json
import re
import struct
import subprocess
from pathlib import Path
from typing import Any

from isco_video_agent.media.ffmpeg import probe
from isco_video_agent.security import secret_free_subprocess_env
from scripts.final_master_acceptance_v2 import seal_final_master_acceptance


SCHEMA_VERSION = 2
EXPECTED_FPS = 30.0
FPS_TOLERANCE = 0.05
OPENING_GRACE_SECONDS = 0.50
BLACK_DETECT_SECONDS = 0.75
BLACK_PIXEL_THRESHOLD = 0.02
BLACK_PICTURE_RATIO = 0.99
SILENCE_DETECT_SECONDS = 4.0
SILENCE_THRESHOLD_DB = -55
FREEZE_DETECT_SECONDS = 8.0
FREEZE_BLOCK_SECONDS = 30.0
FREEZE_NOISE_DB = -60
FULL_SCAN_TIMEOUT_SECONDS = 1200
_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
_HDR_PRIMARIES = {"bt2020"}
_HDR_SPACES = {"bt2020nc", "bt2020c"}


class FinalMasterQCError(RuntimeError):
    pass


def qc_policy_fingerprint() -> str:
    policy = {
        "schema_version": SCHEMA_VERSION,
        "expected_fps": EXPECTED_FPS,
        "fps_tolerance": FPS_TOLERANCE,
        "opening_grace_seconds": OPENING_GRACE_SECONDS,
        "near_black": [BLACK_DETECT_SECONDS, BLACK_PIXEL_THRESHOLD, BLACK_PICTURE_RATIO],
        "silence": [SILENCE_DETECT_SECONDS, SILENCE_THRESHOLD_DB],
        "freeze": [FREEZE_DETECT_SECONDS, FREEZE_BLOCK_SECONDS, FREEZE_NOISE_DB],
        "full_scan_timeout_seconds": FULL_SCAN_TIMEOUT_SECONDS,
        "video": {
            "codec": "h264",
            "pixel_format": "yuv420p",
            "profile": "high_when_reported",
            "field_order": "progressive_when_reported",
            "sdr_color": "bt709_when_reported",
        },
        "audio": {"codec": "aac", "sample_rate": 48000, "channels": [1, 2], "profile": "lc_when_reported"},
        "mp4_fast_start": "warn_if_not_present",
        "long_dimensions": [1920, 1080],
        "short_dimensions": [1080, 1920],
    }
    payload = json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise FinalMasterQCError(f"Missing or invalid final-master QC source: {Path(path).name}") from exc
    if not isinstance(data, dict):
        raise FinalMasterQCError(f"Final-master QC source must be an object: {Path(path).name}")
    return data


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _rate(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    if "/" in text:
        left, right = text.split("/", 1)
        denominator = _float(right)
        return _float(left) / denominator if denominator else 0.0
    return _float(text)


def _event(start: float, end: float, duration: float | None = None) -> dict[str, float]:
    start = max(0.0, float(start))
    end = max(start, float(end))
    return {
        "start_seconds": round(start, 3),
        "end_seconds": round(end, 3),
        "duration_seconds": round(float(duration) if duration is not None else end - start, 3),
    }


def _parse_black_events(stderr: str) -> list[dict[str, float]]:
    pattern = re.compile(
        r"black_start:\s*([0-9.]+)\s+black_end:\s*([0-9.]+)\s+black_duration:\s*([0-9.]+)"
    )
    return [_event(float(a), float(b), float(d)) for a, b, d in pattern.findall(stderr)]


def _parse_silence_events(stderr: str, *, final_seconds: float) -> list[dict[str, float]]:
    token = re.compile(r"silence_(start|end):\s*([0-9.]+)(?:\s*\|\s*silence_duration:\s*([0-9.]+))?")
    open_start: float | None = None
    result: list[dict[str, float]] = []
    for match in token.finditer(stderr):
        kind, raw_value, raw_duration = match.groups()
        value = float(raw_value)
        if kind == "start":
            open_start = value
            continue
        if open_start is None:
            continue
        duration = float(raw_duration) if raw_duration else max(0.0, value - open_start)
        result.append(_event(open_start, value, duration))
        open_start = None
    if open_start is not None and final_seconds > open_start:
        result.append(_event(open_start, final_seconds))
    return result


def _parse_freeze_events(stderr: str, *, final_seconds: float) -> list[dict[str, float]]:
    token = re.compile(r"freeze_(start|duration|end):\s*([0-9.]+)")
    open_start: float | None = None
    pending_duration: float | None = None
    result: list[dict[str, float]] = []
    for match in token.finditer(stderr):
        kind, raw_value = match.groups()
        value = float(raw_value)
        if kind == "start":
            open_start = value
            pending_duration = None
        elif kind == "duration" and open_start is not None:
            pending_duration = value
        elif kind == "end" and open_start is not None:
            result.append(_event(open_start, value, pending_duration))
            open_start = None
            pending_duration = None
    if open_start is not None and final_seconds > open_start:
        result.append(_event(open_start, final_seconds, pending_duration))
    return result


def _run_full_scan(final_path: Path, *, has_audio: bool) -> dict[str, Any]:
    video_filter = (
        f"[0:v:0]blackdetect=d={BLACK_DETECT_SECONDS}:pix_th={BLACK_PIXEL_THRESHOLD}:pic_th={BLACK_PICTURE_RATIO},"
        f"freezedetect=n={FREEZE_NOISE_DB}dB:d={FREEZE_DETECT_SECONDS}[qc_v]"
    )
    filters = video_filter
    maps = ["-map", "[qc_v]"]
    if has_audio:
        filters += f";[0:a:0]silencedetect=n={SILENCE_THRESHOLD_DB}dB:d={SILENCE_DETECT_SECONDS}[qc_a]"
        maps += ["-map", "[qc_a]"]
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-v",
                "info",
                "-xerror",
                "-i",
                str(final_path),
                "-filter_complex",
                filters,
                *maps,
                "-f",
                "null",
                "-",
            ],
            check=False,
            env=secret_free_subprocess_env(),
            capture_output=True,
            text=True,
            timeout=FULL_SCAN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raw_stderr = exc.stderr or ""
        stderr = raw_stderr.decode("utf-8", errors="replace") if isinstance(raw_stderr, bytes) else str(raw_stderr)
        return {
            "returncode": 124,
            "timed_out": True,
            "black_events": _parse_black_events(stderr),
            "silence_events": [],
            "freeze_events": [],
            "stderr": stderr,
        }
    stderr = proc.stderr or ""
    return {
        "returncode": int(proc.returncode),
        "timed_out": False,
        "black_events": _parse_black_events(stderr),
        "silence_events": [],
        "freeze_events": [],
        "stderr": stderr,
    }


def _interior_overlap(event: dict[str, float], *, body_end: float) -> float:
    start = max(float(event["start_seconds"]), OPENING_GRACE_SECONDS)
    end = min(float(event["end_seconds"]), float(body_end))
    return max(0.0, end - start)


def _stream_contract(info: dict[str, Any], fmt: str) -> tuple[dict[str, Any], list[str], list[str]]:
    streams = info.get("streams") if isinstance(info, dict) else None
    if not isinstance(streams, list):
        streams = []
    videos = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"]
    audios = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"]
    blocks: list[str] = []
    warnings: list[str] = []

    if len(videos) != 1:
        blocks.append(f"video_stream_count={len(videos)}")
        video: dict[str, Any] = {}
    else:
        video = videos[0]

    expected_width, expected_height = ((1080, 1920) if fmt == "moment" else (1920, 1080))
    width = int(video.get("width") or 0) if video else 0
    height = int(video.get("height") or 0) if video else 0
    codec = str(video.get("codec_name") or "") if video else ""
    pix_fmt = str(video.get("pix_fmt") or "") if video else ""
    profile = str(video.get("profile") or "").strip() if video else ""
    field_order = str(video.get("field_order") or "").strip().casefold() if video else ""
    fps = _rate(video.get("avg_frame_rate") or video.get("r_frame_rate")) if video else 0.0
    if (width, height) != (expected_width, expected_height):
        blocks.append(f"unexpected_dimensions={width}x{height};expected={expected_width}x{expected_height}")
    if codec != "h264":
        blocks.append(f"unexpected_video_codec={codec or 'missing'}")
    if pix_fmt != "yuv420p":
        blocks.append(f"unexpected_pixel_format={pix_fmt or 'missing'}")
    if abs(fps - EXPECTED_FPS) > FPS_TOLERANCE:
        blocks.append(f"unexpected_fps={fps:.3f}")
    if profile and profile.casefold() != "high":
        blocks.append(f"unexpected_h264_profile={profile}")
    elif not profile:
        warnings.append("h264_profile_missing")
    if field_order and field_order not in {"progressive", "unknown"}:
        blocks.append(f"unexpected_field_order={field_order}")
    elif not field_order:
        warnings.append("field_order_missing")

    color_transfer = str(video.get("color_transfer") or "").casefold() if video else ""
    color_primaries = str(video.get("color_primaries") or "").casefold() if video else ""
    color_space = str(video.get("color_space") or "").casefold() if video else ""
    explicit_hdr = (
        color_transfer in _HDR_TRANSFERS
        or color_primaries in _HDR_PRIMARIES
        or color_space in _HDR_SPACES
    )
    if explicit_hdr:
        blocks.append(
            "unexpected_hdr_master="
            f"transfer:{color_transfer or 'missing'},primaries:{color_primaries or 'missing'},space:{color_space or 'missing'}"
        )
    if not color_transfer or not color_primaries or not color_space:
        warnings.append("color_tags_incomplete_but_no_explicit_hdr_tag")
    elif not explicit_hdr and {color_transfer, color_primaries, color_space} != {"bt709"}:
        blocks.append(
            "unexpected_sdr_color_tags="
            f"transfer:{color_transfer},primaries:{color_primaries},space:{color_space}"
        )

    if len(audios) > 1:
        blocks.append(f"audio_stream_count={len(audios)}")
    audio = audios[0] if audios else {}
    if audio:
        audio_codec = str(audio.get("codec_name") or "")
        audio_profile = str(audio.get("profile") or "").strip()
        sample_rate = int(_float(audio.get("sample_rate")))
        channels = int(audio.get("channels") or 0)
        if audio_codec != "aac":
            blocks.append(f"unexpected_audio_codec={audio_codec or 'missing'}")
        if audio_codec == "aac" and audio_profile and audio_profile.casefold() not in {"lc", "aac lc"}:
            blocks.append(f"unexpected_aac_profile={audio_profile}")
        elif audio_codec == "aac" and not audio_profile:
            warnings.append("aac_profile_missing")
        if sample_rate != 48000:
            blocks.append(f"unexpected_audio_sample_rate={sample_rate}")
        if channels not in {1, 2}:
            blocks.append(f"unexpected_audio_channels={channels}")
    else:
        audio_codec = ""
        audio_profile = ""
        sample_rate = 0
        channels = 0

    return (
        {
            "video_stream_count": len(videos),
            "audio_stream_count": len(audios),
            "video_codec": codec,
            "video_profile": profile or None,
            "field_order": field_order or None,
            "width": width,
            "height": height,
            "pixel_format": pix_fmt,
            "fps": round(fps, 4),
            "color_transfer": color_transfer or None,
            "color_primaries": color_primaries or None,
            "color_space": color_space or None,
            "audio_codec": audio_codec or None,
            "audio_profile": audio_profile or None,
            "audio_sample_rate": sample_rate or None,
            "audio_channels": channels or None,
        },
        blocks,
        warnings,
    )


def _mp4_fast_start(final_path: Path) -> tuple[bool | None, list[str]]:
    positions: dict[str, int] = {}
    order: list[str] = []
    try:
        size = final_path.stat().st_size
        offset = 0
        with final_path.open("rb") as handle:
            while offset + 8 <= size and len(order) < 64:
                handle.seek(offset)
                header = handle.read(8)
                if len(header) != 8:
                    break
                box_size, box_type = struct.unpack(">I4s", header)
                header_size = 8
                if box_size == 1:
                    extended = handle.read(8)
                    if len(extended) != 8:
                        break
                    box_size = struct.unpack(">Q", extended)[0]
                    header_size = 16
                elif box_size == 0:
                    box_size = size - offset
                if box_size < header_size or offset + box_size > size:
                    break
                name = box_type.decode("ascii", errors="replace")
                order.append(name)
                positions.setdefault(name, offset)
                if "moov" in positions and "mdat" in positions:
                    return positions["moov"] < positions["mdat"], order
                offset += int(box_size)
    except OSError:
        return None, order
    if "moov" not in positions or "mdat" not in positions:
        return None, order
    return positions["moov"] < positions["mdat"], order


def run_final_master_qc(output_dir: Path) -> dict[str, Any]:
    root = Path(output_dir)
    final_path = root / "final.mp4"
    plan = _read_object(root / "plan.json")
    quality = _read_object(root / "quality-final.json")
    timeline = _read_object(root / "visual-timeline.json")
    if final_path.is_symlink() or not final_path.is_file():
        raise FinalMasterQCError("Final Master QC requires regular final.mp4")

    fmt = str(plan.get("format") or quality.get("format") or "").strip().lower()
    if fmt not in {"film", "story", "moment"}:
        raise FinalMasterQCError("Final Master QC received unsupported format")
    body_end = _float(timeline.get("duration_seconds"))
    if body_end <= 0:
        raise FinalMasterQCError("Final Master QC requires positive M7 timeline duration")

    info = probe(final_path)
    final_seconds = _float((info.get("format") or {}).get("duration")) if isinstance(info, dict) else 0.0
    if final_seconds <= 0 or final_seconds + 0.25 < body_end:
        raise FinalMasterQCError("Final Master QC found invalid final/body duration relationship")

    stream_contract, blocking, warnings = _stream_contract(info, fmt)
    has_audio = bool(stream_contract["audio_stream_count"])
    scan = _run_full_scan(final_path, has_audio=has_audio)
    stderr = str(scan.pop("stderr", ""))
    scan["silence_events"] = _parse_silence_events(stderr, final_seconds=final_seconds) if has_audio else []
    scan["freeze_events"] = _parse_freeze_events(stderr, final_seconds=final_seconds)

    if scan.get("timed_out") is True:
        blocking.append("full_decode_timeout")
    elif scan["returncode"] != 0:
        blocking.append("full_decode_failed")

    for event in scan["black_events"]:
        overlap = _interior_overlap(event, body_end=body_end)
        if overlap >= BLACK_DETECT_SECONDS:
            blocking.append(
                f"interior_near_black={event['start_seconds']:.3f}-{event['end_seconds']:.3f}s"
            )

    for event in scan["silence_events"]:
        overlap = _interior_overlap(event, body_end=body_end)
        if overlap >= SILENCE_DETECT_SECONDS:
            blocking.append(
                f"interior_audio_dropout={event['start_seconds']:.3f}-{event['end_seconds']:.3f}s"
            )

    freeze_warnings: list[dict[str, float]] = []
    for event in scan["freeze_events"]:
        overlap = _interior_overlap(event, body_end=body_end)
        if overlap >= FREEZE_BLOCK_SECONDS:
            blocking.append(
                f"interior_exact_freeze={event['start_seconds']:.3f}-{event['end_seconds']:.3f}s"
            )
        elif overlap >= FREEZE_DETECT_SECONDS:
            freeze_warnings.append({**event, "interior_overlap_seconds": round(overlap, 3)})
    if freeze_warnings:
        warnings.append("interior_exact_freeze_observed_below_block_threshold")

    fast_start, top_level_boxes = _mp4_fast_start(final_path)
    if fast_start is False:
        warnings.append("mp4_fast_start_not_present")
    elif fast_start is None:
        warnings.append("mp4_fast_start_not_observable")

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "block" if blocking else "pass",
        "production_stage": "post_render_pre_gold_acceptance",
        "final_file": final_path.name,
        "format": fmt,
        "final_duration_seconds": round(final_seconds, 3),
        "m7_body_duration_seconds": round(body_end, 3),
        "outro_or_tail_seconds": round(max(0.0, final_seconds - body_end), 3),
        "full_decode_ok": scan["returncode"] == 0 and scan.get("timed_out") is not True,
        "full_decode_timed_out": scan.get("timed_out") is True,
        "full_decode_timeout_seconds": FULL_SCAN_TIMEOUT_SECONDS,
        "stream_contract": stream_contract,
        "upload_conformance": {
            "mp4_fast_start": fast_start,
            "top_level_boxes_observed": top_level_boxes,
            "h264_high_profile": (stream_contract.get("video_profile") or "").casefold() == "high",
            "progressive": (stream_contract.get("field_order") or "").casefold() in {"progressive", "unknown"},
            "aac_lc": (
                not has_audio
                or (stream_contract.get("audio_profile") or "").casefold() in {"lc", "aac lc"}
            ),
            "bt709": {
                stream_contract.get("color_transfer"),
                stream_contract.get("color_primaries"),
                stream_contract.get("color_space"),
            } == {"bt709"},
        },
        "detectors": {
            "near_black": {
                "minimum_seconds": BLACK_DETECT_SECONDS,
                "pixel_threshold": BLACK_PIXEL_THRESHOLD,
                "picture_ratio": BLACK_PICTURE_RATIO,
                "events": scan["black_events"],
            },
            "audio_silence": {
                "minimum_seconds": SILENCE_DETECT_SECONDS,
                "threshold_db": SILENCE_THRESHOLD_DB,
                "events": scan["silence_events"],
            },
            "exact_freeze": {
                "observe_seconds": FREEZE_DETECT_SECONDS,
                "block_seconds": FREEZE_BLOCK_SECONDS,
                "events": scan["freeze_events"],
                "below_block_threshold": freeze_warnings,
            },
        },
        "opening_grace_seconds": OPENING_GRACE_SECONDS,
        "outro_excluded_from_perceptual_blocks": True,
        "blocking_findings": blocking,
        "warnings": warnings,
        "existing_quality_authorities_preserved": [
            "engine_quality_final_duration",
            "engine_quality_final_loudness_true_peak",
            "engine_quality_final_av_sync",
            "gold_final_critic",
        ],
        "ai_calls_added": 0,
        "final_media_mutated": False,
    }
    report = seal_final_master_acceptance(
        root,
        report,
        policy_fingerprint=qc_policy_fingerprint(),
    )
    if blocking:
        raise FinalMasterQCError("Final Master QC blocked release; inspect final-master-qc.json")
    return report