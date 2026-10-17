"""Owner-designed images for the owner Podcast: visuals, light motion and the cover.

The owner generates one designed image per topic (plus a cover) with his own tool and
sends them in Telegram. The workflow downloads them into ``CLEAN_V2_OWNER_ART_DIR`` as
``image-1.jpg ... image-N.jpg`` (1920x1080) and optionally ``cover.jpg``. This source
replaces stock search for the Podcast only: each visual beat becomes a slow, light
drift over one of those images. No provider call, no review, no replacement.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

ART_DIR_ENV = "CLEAN_V2_OWNER_ART_DIR"
PROVIDER = "owner_art"
WIDTH, HEIGHT = 1920, 1080
COVER_SIZE = (1280, 720)
CLIP_WIDTH, CLIP_HEIGHT = 1280, 720  # the Podcast render size; no point encoding more
_IMAGE_RE = re.compile(r"^image-(\d+)\.jpg$")


def art_dir() -> Path | None:
    value = os.environ.get(ART_DIR_ENV, "").strip()
    if not value:
        return None
    path = Path(value)
    return path if path.is_dir() else None


def body_images(directory: Path) -> list[Path]:
    found = [(int(m.group(1)), p) for p in directory.iterdir() if (m := _IMAGE_RE.match(p.name))]
    return [p for _, p in sorted(found)]


def cover_image(directory: Path) -> Path | None:
    path = directory / "cover.jpg"
    return path if path.is_file() else None


def normalize_image(source: Path, destination: Path, size: tuple[int, int] = (WIDTH, HEIGHT)) -> Path:
    """Centre-crop to the exact aspect, resize, save as a high-quality JPEG."""
    from PIL import Image, ImageOps

    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    width, height = size
    scale = max(width / image.width, height / image.height)
    resized = image.resize((max(width, round(image.width * scale)), max(height, round(image.height * scale))), Image.LANCZOS)
    left, top = (resized.width - width) // 2, (resized.height - height) // 2
    resized.crop((left, top, left + width, top + height)).save(destination, "JPEG", quality=94, subsampling=0)
    return destination


def make_motion_clip(
    image: Path, destination: Path, *, seconds: float, direction: int, film: bool = False, motion: float = 1.0,
    portrait: bool = False, motion_kind: str = "slow_pan",
) -> Path:
    """Slow drift across a slightly enlarged image (cheap: a crop, no per-frame scaling).

    Podcast: two gentle diagonal moves. Film: four distinct moves over a larger margin, so
    consecutive images never move the same way.
    """
    from .media import _run

    seconds = max(2.0, float(seconds))
    width, height = (720, 1280) if portrait else (CLIP_WIDTH, CLIP_HEIGHT)
    extra_w, extra_h = (176, 98) if film else (112, 62)
    if film:
        extra_w, extra_h = round(extra_w * motion / 2) * 2, round(extra_h * motion / 2) * 2
    big_w, big_h = width + extra_w, height + extra_h
    forward = f"t/{seconds:.3f}"
    backward = f"(1-t/{seconds:.3f})"
    style = direction % (4 if film else 2)
    x, y = {
        0: (f"{extra_w}*{forward}", f"{extra_h}*{backward}"),   # up-right
        1: (f"{extra_w}*{backward}", f"{extra_h}*{forward}"),   # down-left
        2: (f"{extra_w}*{forward}", f"{extra_h}/2"),            # straight right
        3: (f"{extra_w}/2", f"{extra_h}*{forward}"),            # straight down
    }[style]
    vf = (
        f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}:x='{x}':y='{y}',"
        "setsar=1,fps=30,format=yuv420p"
    )
    if motion_kind == "slow_zoom":
        vf = (f"scale={width*2}:{height*2}:force_original_aspect_ratio=increase,"
              f"zoompan=z='1.02+0.04*on/{round(seconds*30)}':x='iw/2-iw/zoom/2':"
              f"y='ih/2-ih/zoom/2':d=1:s={width}x{height}:fps=30,setsar=1,format=yuv420p")
    elif motion_kind == "hold":
        vf = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,fps=30,format=yuv420p"
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-loop", "1", "-framerate", "30", "-t", f"{seconds:.3f}", "-i", str(image),
            "-vf", vf, "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
            "-pix_fmt", "yuv420p", str(destination),
        ],
        timeout=600,
    )
    return destination


# Film pace by chosen type: (shot-length factor, camera-move factor). Not an engine per type,
# just how long a picture is held and how far it drifts. Every other type is 1.0 / 1.0.
PACE_BY_TYPE = {
    "paradox": (1.25, 0.8),
    "inner_dialogue": (1.25, 0.8),
    "story_analysis": (1.15, 0.9),
    "connected_list": (0.8, 1.2),
    "hypothesis_test": (0.95, 1.0),
}
_SHOT_PATTERN = (8.0, 6.0, 10.0, 7.0, 11.0, 9.0)  # a rhythm, not a rule: nothing is capped to it
DISSOLVE_SECONDS = 0.5


def film_pace(pack_type: str | None) -> tuple[float, float]:
    return PACE_BY_TYPE.get(str(pack_type or ""), (1.0, 1.0))


def segment_plan(seconds: float, start_index: int, factor: float = 1.0) -> list[float]:
    """Varied shot lengths that sum to ``seconds``; a leftover tail is absorbed, never a flash."""
    remaining = max(2.0, float(seconds))
    lengths: list[float] = []
    step = 0
    while remaining > 0.01:
        target = _SHOT_PATTERN[(start_index + step) % len(_SHOT_PATTERN)] * factor
        take = remaining if remaining - target < 0.6 * target else target
        lengths.append(take)
        remaining -= take
        step += 1
    return lengths


def make_sequence_clip(
    images: list[Path], destination: Path, *, seconds: float, start_index: int,
    factor: float = 1.0, motion: float = 1.0,
) -> tuple[Path, int]:
    """Film beat: several pictures of varied length, each with its own move, joined by a short dissolve.

    Returns the clip and how many pictures it consumed.
    """
    from .media import _run

    lengths = segment_plan(seconds, start_index, factor)
    parts: list[Path] = []
    for offset, length in enumerate(lengths):
        index = start_index + offset
        extra = DISSOLVE_SECONDS if offset < len(lengths) - 1 else 0.0
        parts.append(
            make_motion_clip(
                images[index % len(images)],
                destination.with_name(f"{destination.stem}-part{offset + 1}.mp4"),
                seconds=length + extra,
                direction=index,
                film=True,
                motion=motion,
            )
        )
    if len(parts) == 1:
        parts[0].replace(destination)
        return destination, 1
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for part in parts:
        command += ["-i", str(part)]
    filters, label, elapsed = [], "[0:v]", 0.0
    for position in range(1, len(parts)):
        elapsed += lengths[position - 1]
        out = f"[x{position}]"
        filters.append(f"{label}[{position}:v]xfade=transition=fade:duration={DISSOLVE_SECONDS}:offset={elapsed:.3f}{out}")
        label = out
    command += [
        "-filter_complex", ";".join(filters), "-map", label, "-an",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22", "-pix_fmt", "yuv420p", str(destination),
    ]
    _run(command, timeout=900)
    for part in parts:
        part.unlink(missing_ok=True)
    return destination, len(parts)


class OwnerArtVisualSource:
    """Wraps the stock source; only the Podcast with owner images is taken over."""

    def __init__(self, inner: Any, directory: Path) -> None:
        self._inner = inner
        self._directory = Path(directory)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @staticmethod
    def _pack_type() -> str | None:
        """The «نوع:» the owner chose for his film, if any (read from the same pack the run uses)."""
        try:
            from .owner_pack import active_pack_path

            path = active_pack_path("film")
            return str(json.loads(path.read_text(encoding="utf-8")).get("type") or "") if path else None
        except (OSError, ValueError, ImportError):
            return None

    def acquire(
        self,
        plan: Mapping[str, Any],
        output_dir: Path,
        fmt: str,
        max_visuals: int,
        section_estimated_seconds: Mapping[str, float] | None = None,
    ) -> tuple[list[Path], list[dict[str, Any]]]:
        if (self._directory / "production.json").is_file():
            return self._acquire_ready(plan, output_dir, fmt, section_estimated_seconds)
        images = body_images(self._directory) if fmt in {"podcast", "film"} else []
        if not images:
            return self._inner.acquire(plan, output_dir, fmt, max_visuals, section_estimated_seconds)
        output_dir.mkdir(parents=True, exist_ok=True)
        estimates = dict(section_estimated_seconds or {})
        sections = [s for s in (plan.get("sections") or []) if isinstance(s, Mapping)]
        story = plan.get("visual_story")
        beats = [b for b in ((story or {}).get("beats") or []) if isinstance(b, Mapping)] if isinstance(story, Mapping) else []
        clips: list[Path] = []
        rights: list[dict[str, Any]] = []
        cursor = 0
        per_section: dict[str, int] = {}
        for beat in beats:
            section_id = str(beat.get("section_id") or "")
            per_section[section_id] = per_section.get(section_id, 0) + 1
        for index, beat in enumerate(beats):
            section_id = str(beat.get("section_id") or "")
            beat_id = str(beat.get("id") or f"b{index + 1}")
            image = images[index % len(images)]
            seconds = float(estimates.get(section_id) or 60.0) / max(1, per_section.get(section_id, 1)) + 6.0
            clip_path = output_dir / f"visual-owner-art-{beat_id}.mp4"
            if fmt == "film":
                factor, motion = film_pace(self._pack_type())
                clip, used = make_sequence_clip(
                    images, clip_path, seconds=seconds, start_index=cursor, factor=factor, motion=motion
                )
                cursor += used
            else:
                clip = make_motion_clip(image, clip_path, seconds=seconds, direction=index)
            transform = getattr(self._inner, "media_transform", None)
            if transform is not None:
                clip = Path(transform(clip))
            clips.append(clip)
            rights.append({
                "provider": PROVIDER,
                "asset_id": f"owner-art-{image.stem}-{beat_id}",
                "source_url": None,
                "creator": "Isco (owner designed)",
                "creator_url": None,
                "query": None,
                "local_file": clip.name,
                "section_id": section_id,
                "beat_id": beat_id,
                "viewer_intent": str(beat.get("viewer_intent") or ""),
                "meaning_target": str(beat.get("meaning_target") or ""),
                "shot_intent": str(beat.get("shot_intent") or ""),
                "writer_anchor_ar": str(beat.get("writer_anchor_ar") or ""),
                "display_text_ar": "",
                "role": str(beat.get("role") or ""),
                "source_actual": PROVIDER,
                "shot_role": str(beat.get("shot_role") or ""),
                "hold_reason": str(beat.get("hold_reason") or ""),
                "pause_intent": str(beat.get("pause_intent") or ""),
                "audio_energy": str(beat.get("audio_energy") or ""),
                "attribution_required": False,
            })
        if not clips:
            return self._inner.acquire(plan, output_dir, fmt, max_visuals, section_estimated_seconds)
        return clips, rights

    def _acquire_ready(self, plan, output_dir, fmt, estimates):
        """One existing motion clip per approved beat; never fall back to stock."""
        from .production_package import validate_manifest
        from .ready_production import continues_previous

        doc = json.loads((self._directory / "production.json").read_text(encoding="utf-8"))
        validate_manifest(doc, {str(p.relative_to(self._directory)) for p in self._directory.rglob("*") if p.is_file()})
        beats = plan["visual_story"]["beats"]
        clips, rights = [], []
        output_dir.mkdir(parents=True, exist_ok=True)
        for index, beat in enumerate(beats):
            asset = self._directory / beat["owner_asset"]
            if not asset.is_file():
                raise RuntimeError("approved_package_image_missing:" + beat["owner_asset"])
            # The shared renderer trims these sources to its measured audio slots.
            first = index
            while first > 0 and continues_previous(beats[first]):
                first -= 1
            last = first + 1
            while last < len(beats) and continues_previous(beats[last]):
                last += 1
            sections = {b["section_id"] for b in beats[first:last]}
            seconds = sum(float((estimates or {}).get(sid) or 60) for sid in sections) + 1
            clip = make_motion_clip(asset, output_dir / f"visual-owner-{beat['id']}.mp4", seconds=seconds,
                                    direction=index, portrait=fmt == "short", motion_kind=beat["owner_motion"])
            clips.append(clip)
            rights.append({**dict(beat), "beat_id": beat["id"], "provider": PROVIDER,
                           "asset_id": beat["owner_asset"], "local_file": clip.name, "source_actual": PROVIDER,
                           "owner_approved": True, "creator": "owner supplied", "attribution_required": False})
        if not clips:
            raise RuntimeError("approved_package_has_no_images")
        return clips, rights


def apply_owner_cover(cover_path: Path, report: dict[str, Any]) -> dict[str, Any]:
    """Replace the generated cover with the cover the owner designed, if he sent one."""
    directory = art_dir()
    if directory and (directory / "production.json").is_file():
        doc = json.loads((directory / "production.json").read_text(encoding="utf-8"))
        source = directory / doc["cover"]["asset"] if doc["cover"]["asset"] else None
        text = str(doc["cover"].get("text") or "").strip()
        if source is None and text and doc.get("scenes"):
            # No cover picture in the package: draw the approved cover text on the
            # first approved scene instead of the topic-less navy fallback, which
            # also replaced the author's cover text with the script title.
            try:
                from .cover_studio import render_cover_studio

                scene = directory / doc["scenes"][0]["asset"]
                rendered = render_cover_studio(scene, Path(cover_path), text=text, fmt=doc["format"])
                return {**report, "status": "pass", "source": "owner_cover_image", "cover_text": text,
                        "selection": "approved_first_scene_with_cover_text",
                        "provider_calls_added": 0, **{k: v for k, v in rendered.items() if k not in report}}
            except Exception as exc:  # fail-soft: keep the generated cover
                report = {**report, "owner_scene_cover": f"unavailable:{type(exc).__name__}:{str(exc)[:120]}"}
        if source is not None:
            normalize_image(source, Path(cover_path), (720, 1280) if doc["format"] == "short" else COVER_SIZE)
            return {**report, "status": "pass", "source": "owner_cover_image", "provider_calls_added": 0}
    source = cover_image(directory) if directory else None
    if source is None:
        return report
    normalize_image(source, Path(cover_path), COVER_SIZE)
    return {**report, "status": "pass", "source": "owner_cover_image", "provider_calls_added": 0}
