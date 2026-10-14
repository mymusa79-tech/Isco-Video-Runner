"""Human scene review for Shorts (Simple Mode picker, V1).

Phase 1 (``CLEAN_V2_SCENE_REVIEW=collect``): after the voice is saved the run
collects up to six stock candidates per visual beat, renders one contact-sheet
image per beat, writes ``scene-review.json`` and stops cleanly. No video is
rendered and nothing is published.

Phase 2 (``CLEAN_V2_SCENE_CHOICES=<file>``): the existing resume-from-voice run
re-acquires visuals. A beat the owner chose uses exactly that stock asset (it
still passes the normal security/transform admission); ``0`` uses the channel
background; a beat the owner left alone keeps the automatic pick.

This module adds no model call, no new source and no new quality gate.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .contracts import atomic_write_json

COLLECT_ENV = "CLEAN_V2_SCENE_REVIEW"
CHOICES_ENV = "CLEAN_V2_SCENE_CHOICES"
REVIEW_FILE = "scene-review.json"
REVIEW_DIR = "scene-review"
CANDIDATES_PER_BEAT = 6
SUPPORTED_FORMAT = "short"
PENDING_STATUS = "awaiting_scene_review"

_CANDIDATE_KEYS = (
    "provider",
    "asset_id",
    "download_url",
    "source_url",
    "creator",
    "creator_url",
    "query",
    "local_rank_score",
    "metadata_semantic_score",
    "retrieval_description",
)


class SceneReviewPending(Exception):
    """Raised after the review pack is ready; the run stops cleanly (exit 0)."""


def collect_mode(fmt: str) -> bool:
    return (
        str(fmt) == SUPPORTED_FORMAT
        and os.environ.get(COLLECT_ENV, "").strip() == "collect"
    )


def load_choices(fmt: str) -> dict[str, Any] | None:
    """Return the owner's saved decision for this run, or None when absent/invalid."""
    if str(fmt) != SUPPORTED_FORMAT:
        return None
    raw = os.environ.get(CHOICES_ENV, "").strip()
    if not raw:
        return None
    try:
        value = json.loads(Path(raw).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return None
    choices = value.get("choices")
    if not isinstance(choices, dict):
        choices = {}
    clean: dict[str, dict[str, Any]] = {}
    for beat_id, entry in choices.items():
        if not isinstance(entry, dict):
            continue
        mode = entry.get("mode")
        if mode == "background":
            clean[str(beat_id)] = {"mode": "background"}
        elif mode == "candidate" and isinstance(entry.get("candidate"), dict):
            candidate = {
                key: entry["candidate"].get(key)
                for key in _CANDIDATE_KEYS
                if key in entry["candidate"]
            }
            if candidate.get("provider") and candidate.get("asset_id") and candidate.get("download_url"):
                clean[str(beat_id)] = {"mode": "candidate", "candidate": candidate}
    return {
        "review_id": str(value.get("review_id") or ""),
        "auto_all": bool(value.get("auto_all")),
        "choices": clean,
    }


def public_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {key: candidate.get(key) for key in _CANDIDATE_KEYS if key in candidate}


def _frame(video: Path, destination: Path) -> bool:
    for seek in ("1.0", "0.2"):
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-ss", seek, "-i", str(video),
                "-frames:v", "1", "-vf", "scale=270:480:force_original_aspect_ratio=increase,crop=270:480",
                str(destination),
            ],
            capture_output=True,
            timeout=60,
        )
        if proc.returncode == 0 and destination.is_file() and destination.stat().st_size > 0:
            return True
    return False


def _contact_sheet(thumbs: list[tuple[int, Path]], destination: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    cell_w, cell_h, gap = 270, 480, 6
    rows = 2 if len(thumbs) > 3 else 1
    sheet = Image.new("RGB", (cell_w * 3 + gap * 2, cell_h * rows + gap * (rows - 1)), (18, 18, 18))
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 54)
    except OSError:
        font = ImageFont.load_default(size=54)
    draw = ImageDraw.Draw(sheet)
    for slot, (number, path) in enumerate(thumbs):
        x = (slot % 3) * (cell_w + gap)
        y = (slot // 3) * (cell_h + gap)
        with Image.open(path) as frame:
            sheet.paste(frame.convert("RGB").resize((cell_w, cell_h)), (x, y))
        draw.rectangle([x, y, x + 74, y + 74], fill=(0, 0, 0))
        draw.text((x + 18, y + 6), str(number), fill=(255, 255, 255), font=font)
    sheet.save(destination, "JPEG", quality=84)


def collect(source: Any, plan: Mapping[str, Any], visuals_dir: Path, fmt: str,
            max_visuals: int, output_dir: Path) -> dict[str, Any]:
    """Run the beat loop in collect mode and write the owner-facing review pack."""
    from .media import _download_media

    source.review_pack = []
    source.acquire(plan, visuals_dir, fmt, max_visuals)
    review_dir = Path(output_dir) / REVIEW_DIR
    review_dir.mkdir(parents=True, exist_ok=True)
    beats_out: list[dict[str, Any]] = []
    for position, beat in enumerate(getattr(source, "review_pack", []), start=1):
        thumbs: list[tuple[int, Path]] = []
        kept: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="scene-review-") as tmp:
            for candidate in beat.get("candidates", []):
                if len(kept) >= CANDIDATES_PER_BEAT:
                    break
                clip = Path(tmp) / f"c{len(kept) + 1}.mp4"
                thumb = Path(tmp) / f"c{len(kept) + 1}.jpg"
                try:
                    _download_media(str(candidate["download_url"]), clip)
                    if not _frame(clip, thumb):
                        continue
                except Exception:
                    continue
                finally:
                    clip.unlink(missing_ok=True)
                number = len(kept) + 1
                kept.append({"n": number, **public_candidate(candidate)})
                kept_thumb = review_dir / f"{beat['id']}-c{number}.jpg"
                thumb.replace(kept_thumb)
                thumbs.append((number, kept_thumb))
            sheet_name = ""
            if thumbs:
                sheet_name = f"{beat['id']}.jpg"
                _contact_sheet(thumbs, review_dir / sheet_name)
        for _, path in thumbs:
            path.unlink(missing_ok=True)
        beats_out.append(
            {
                "id": beat["id"],
                "index": position,
                "role": beat.get("role", ""),
                "text_ar": beat.get("text_ar", ""),
                "shot_intent": beat.get("shot_intent", ""),
                "sheet": sheet_name,
                "candidates": kept,
            }
        )
    pack = {
        "schema_version": 1,
        "review_id": secrets.token_hex(4),
        "format": fmt,
        "beats": beats_out,
    }
    atomic_write_json(Path(output_dir) / REVIEW_FILE, pack)
    return pack


def report_block(output_dir: Path) -> dict[str, Any]:
    """Honest human-review summary derived only from recorded rights rows."""
    try:
        rows = json.loads((Path(output_dir) / "rights-manifest.json").read_text(encoding="utf-8")).get("assets") or []
    except (OSError, ValueError):
        rows = []
    marks = [str(r.get("human_choice") or "") for r in rows if isinstance(r, dict) and not r.get("story_beat_auxiliary")]
    if not any(marks):
        return {"mode": "not_applicable"}
    chosen = marks.count("candidate")
    background = marks.count("background")
    auto = marks.count("auto")
    return {
        "mode": "human_reviewed" if (chosen or background) else "auto_all_by_owner_decision",
        "human_chosen_clips": chosen,
        "human_background": background,
        "kept_automatic": auto,
    }
