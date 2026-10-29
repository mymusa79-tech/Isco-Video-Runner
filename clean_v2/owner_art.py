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
PINNED_MIN_SECONDS = 1.5   # a pinned clip shorter than this is not worth a scene
PINNED_GRAYSCALE_MAX_CHROMA = 0.035  # mean (max-min)/255 of sampled pixels; below this a stock clip is black-and-white
GRADE_PROFILES = {  # one gentle look per format so stock and the editor's images sit together
    "podcast": "null",  # documentary photos retain their own natural colour
    "film": "eq=contrast=1.04:saturation=0.90,colorbalance=rs=-0.01:bs=0.02",
    "short": "eq=contrast=1.05:saturation=0.92,colorbalance=rs=-0.01:bs=0.02",
    "ala_mahl": "eq=contrast=1.02:saturation=0.90,colorbalance=rs=0.04:bs=-0.04",
}
PINNED_FADE_SECONDS = 0.8  # dissolve from a short pinned clip into the approved image
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
    import shutil

    # Preserve the editor's exact JPEG pixels and metadata when no resize is needed.
    with Image.open(source) as opened:
        if (source.suffix.lower() in {".jpg", ".jpeg"} and opened.size == size
                and opened.getexif().get(274, 1) == 1):
            if source.resolve() != destination.resolve():
                shutil.copyfile(source, destination)
            return destination

    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    width, height = size
    scale = max(width / image.width, height / image.height)
    resized = image.resize((max(width, round(image.width * scale)), max(height, round(image.height * scale))), Image.LANCZOS)
    left, top = (resized.width - width) // 2, (resized.height - height) // 2
    resized.crop((left, top, left + width, top + height)).save(destination, "JPEG", quality=94, subsampling=0)
    return destination


# Camera moves are rendered from a 3x supersampled copy of the picture. The old version cropped at whole
# output pixels (and the 720x1280 clip is later enlarged 1.5x), so a slow pan advanced in irregular 1-2 px
# jumps and a zoom wobbled by a pixel: a visible tremor. Measured on a real Short (phase correlation):
# pan residual 0.29 px (old) -> 0.09 px (3x), at about 1.8x the encode time of a still clip.
MOTION_SUPERSAMPLE = 3
HOLD_PUSH = 0.035         # "hold" still breathes: a barely visible push-in over the shot
ZOOM_PUSH = 0.10          # slow_zoom: a clear but gentle push-in
PUNCH_PUSH = 0.04         # an editor's punch-in on a long still shot
PUNCH_SECONDS = 0.45
PUNCH_EVERY = 7.0         # a long still gets a punch-in about every 7 s, never faster


def _smooth(expr: str) -> str:
    return f"({expr})*({expr})*(3-2*({expr}))"


def punch_times(seconds: float) -> list[float]:
    """Moments (s) of the editor-style punch-ins for a still held this long; none on a short shot."""
    count = min(3, int(float(seconds) // PUNCH_EVERY))
    return [round(float(seconds) * (index + 1) / (count + 1), 3) for index in range(count)]


def make_motion_clip(
    image: Path, destination: Path, *, seconds: float, direction: int, film: bool = False, motion: float = 1.0,
    portrait: bool = False, motion_kind: str = "slow_pan",
    respect_hold: bool = False,
) -> Path:
    """A picture with a smooth, sub-pixel camera move.

    slow_pan: drift across a slightly enlarged image. Podcast: two gentle diagonal moves; film:
    four distinct moves over a larger margin, so consecutive images never move the same way.
    slow_zoom: a push-in. Legacy hold: a barely visible push-in.
    With respect_hold=True (approved ZIP only), 'hold' means truly STATIC:
    never zoom, pan or add timed punch-ins. Non-ZIP/legacy behavior is unchanged.
    Other hold / zoom shots longer than about 7 s retain legacy punch-ins.
    """
    from .media import _run

    seconds = max(2.0, float(seconds))
    width, height = (720, 1280) if portrait else (CLIP_WIDTH, CLIP_HEIGHT)
    S = MOTION_SUPERSAMPLE
    frames = max(2, round(seconds * 30))
    progress = f"min(1,on/{frames - 1})"
    eased = f"({progress})*({progress})*(3-2*({progress}))"
    centre_x, centre_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if motion_kind == "slow_zoom":
        zoom, x, y = f"1.0+{ZOOM_PUSH}*{eased}", centre_x, centre_y
    elif motion_kind == "hold":
        zoom, x, y = (
            ("1.0", "0", "0") if respect_hold
            else (f"1.0+{HOLD_PUSH}*{eased}", centre_x, centre_y)
        )
    else:
        extra_w, extra_h = (176, 98) if film else (112, 62)
        if film:
            extra_w, extra_h = round(extra_w * motion / 2) * 2, round(extra_h * motion / 2) * 2
        zoom = f"{(width + extra_w) / width:.5f}"
        ratio = (height + extra_h) / height
        # a pan keeps one zoom; the vertical room comes from the (slightly larger) vertical share
        fx, fy = "(iw-iw/zoom)", f"(ih-ih/zoom)*{min(1.0, (ratio - 1) / max(1e-6, (width + extra_w) / width - 1)):.4f}"
        style = direction % (4 if film else 2)
        x, y = {
            0: (f"{fx}*{eased}", f"{fy}*(1-{eased})"),    # up-right
            1: (f"{fx}*(1-{eased})", f"{fy}*{eased}"),    # down-left
            2: (f"{fx}*{eased}", f"{fy}/2"),              # straight right
            3: (f"{fx}/2", f"{fy}*{eased}"),              # straight down
        }[style]
    if motion_kind in {"hold", "slow_zoom"} and not (respect_hold and motion_kind == "hold"):
        for moment in punch_times(seconds):
            u = f"clip((on/30-{moment:.3f})/{PUNCH_SECONDS},0,1)"
            zoom += f"+{PUNCH_PUSH}*({u})*({u})*(3-2*({u}))"
    vf = (
        f"scale={width * S}:{height * S}:force_original_aspect_ratio=increase:flags=bicubic,"
        f"crop={width * S}:{height * S},"
        f"zoompan=z='{zoom}':x='{x}':y='{y}':d=1:s={width}x{height}:fps=30,"
        "setsar=1,format=yuv420p"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-loop", "1", "-framerate", "30", "-t", f"{seconds:.3f}", "-i", str(image),
            "-vf", vf, "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
            "-pix_fmt", "yuv420p", str(destination),
        ],
        timeout=1800,
    )
    return destination


# The eight owner Film types have distinct, restrained pacing. This remains the
# SAME image sequencer: the type changes rhythm, not media ownership or narration.
PACE_BY_TYPE = {
    "direct_cinematic": (1.0, 1.0),
    "question_answer": (1.08, 0.82),
    "inner_dialogue": (1.25, 0.8),
    "problem_reveal_solution": (0.94, 1.04),
    "story_analysis": (1.15, 0.9),
    "paradox": (1.25, 0.8),
    "hypothesis_test": (0.95, 1.0),
    "connected_list": (0.8, 1.2),
}
_SHOT_PATTERN = (8.0, 6.0, 10.0, 7.0, 11.0, 9.0)
# Different sentence structures need different hold/reveal patterns. The values
# are base seconds and never force a cut in an approved ZIP beat.
SHOT_PATTERNS_BY_TYPE = {
    "direct_cinematic": _SHOT_PATTERN,
    "question_answer": (9.0, 5.0, 11.0, 7.0),
    "inner_dialogue": (12.0, 10.0, 7.0, 11.0),
    "problem_reveal_solution": (7.0, 10.0, 6.0, 12.0),
    "story_analysis": (10.0, 8.0, 12.0, 6.0),
    "paradox": (10.0, 6.0, 10.0, 7.0),
    "hypothesis_test": (9.0, 9.0, 6.0, 10.0),
    "connected_list": (6.0, 8.0, 5.0, 7.0),
}
DISSOLVE_SECONDS = 0.5


def film_pace(pack_type: str | None) -> tuple[float, float]:
    return PACE_BY_TYPE.get(str(pack_type or ""), (1.0, 1.0))


def segment_plan(
    seconds: float, start_index: int, factor: float = 1.0,
    shot_pattern: tuple[float, ...] | None = None,
) -> list[float]:
    """Varied shot lengths that sum to ``seconds``; a leftover tail is absorbed, never a flash."""
    pattern = shot_pattern or _SHOT_PATTERN
    remaining = max(2.0, float(seconds))
    lengths: list[float] = []
    step = 0
    while remaining > 0.01:
        target = pattern[(start_index + step) % len(pattern)] * factor
        take = remaining if remaining - target < 0.6 * target else target
        lengths.append(take)
        remaining -= take
        step += 1
    return lengths


def make_sequence_clip(
    images: list[Path], destination: Path, *, seconds: float, start_index: int,
    factor: float = 1.0, motion: float = 1.0,
    shot_pattern: tuple[float, ...] | None = None,
) -> tuple[Path, int]:
    """Film beat: several pictures of varied length, each with its own move, joined by a short dissolve.

    Returns the clip and how many pictures it consumed.
    """
    from .media import _run

    lengths = segment_plan(seconds, start_index, factor, shot_pattern)
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



def clip_is_grayscale(clip: Path, *, length: float) -> bool:
    """True when a stock video is black-and-white (sampled frames carry almost no colour).

    Colour unity is part of the stock brief: a monochrome clip inside a colour
    scene is rejected so the next linked alternative is tried. Fail-soft: any
    probe problem returns False and never blocks a clip.
    """
    import subprocess

    try:
        rate = max(0.2, 6.0 / max(1.0, float(length)))
        raw = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(clip), "-an",
             "-vf", f"fps={rate:.4f},scale=32:32,format=rgb24", "-frames:v", "8",
             "-f", "rawvideo", "-"],
            capture_output=True, timeout=120, check=True).stdout
    except Exception:
        return False
    pixels = len(raw) // 3
    if pixels < 32 * 32:
        return False
    spread = 0
    for i in range(0, pixels * 3, 3):
        r, g, b = raw[i], raw[i + 1], raw[i + 2]
        spread += max(r, g, b) - min(r, g, b)
    return spread / (pixels * 255.0) < PINNED_GRAYSCALE_MAX_CHROMA


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
                    images, clip_path, seconds=seconds, start_index=cursor, factor=factor,
                    motion=motion, shot_pattern=SHOT_PATTERNS_BY_TYPE.get(self._pack_type()),
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

    def _try_stock(self, beat: Mapping[str, Any], plan: Mapping[str, Any], output_dir: Path, fmt: str):
        """One real stock clip for this beat, or (None, reason) so the approved image is used.

        Uses the existing stock layer for exactly this beat. The free-AI route is closed here
        (the approved image IS the fallback), and a clip is admitted only when it is real stock
        whose own descriptive metadata passes the conservative semantic check. Anything else,
        including any exception, falls back to the approved image; this never blocks production.
        """
        from .media import _stock_review_semantic_fit

        stock = dict(beat["owner_stock"])
        query, alternate = stock["query_en"], stock.get("query_alt_en", "")
        single = {k: v for k, v in beat.items() if k not in {"owner_asset", "owner_motion", "owner_transition", "owner_stock"}}
        single.update(stock_query_en=query, stock_query_alt_en=alternate, source_preference="stock_motion",
                      _ai_fallback_pass=True)
        sub_plan = {
            "sections": [{"id": beat["section_id"], "visual_query_en": query, "visual_query_alt_en": alternate}],
            "visual_story": {"beats": [single]},
        }
        # A narrower query can fail although the approved alternative would find the
        # requested action. Retry ONCE with the editor-supplied alternate only; never
        # invent a new search or admit a clip on weak visual metadata.
        queries = [query]
        if alternate.strip() and alternate.strip().casefold() != query.strip().casefold():
            queries.append(alternate.strip())
        reason = "stock_no_single_clip"
        for attempt, active_query in enumerate(queries):
            single["stock_query_en"] = active_query
            single["stock_query_alt_en"] = ""
            sub_plan["sections"][0]["visual_query_en"] = active_query
            sub_plan["sections"][0]["visual_query_alt_en"] = ""
            workdir = Path(output_dir) / f"stock-{beat['id']}-q{attempt + 1}"
            try:
                found_clips, found_rights = self._inner.acquire(sub_plan, workdir, fmt, 1, None)
            except Exception as exc:  # approved artwork is always the safe fallback
                reason = f"stock_error:{type(exc).__name__}"
                continue
            if len(found_clips) != 1 or len(found_rights) != 1:
                reason = "stock_no_single_clip"
                continue
            row = dict(found_rights[0])
            if str(row.get("source_actual") or "") not in {"stock_motion", "stock_still"}:
                reason = "no_admitted_stock_candidate"
                continue
            # Query, alternate and must_show are reviewed with the same strict test.
            wanted = [query, alternate, str(stock.get("must_show") or "")]
            if not any(text.strip() and _stock_review_semantic_fit(text, row) for text in wanted):
                reason = "stock_metadata_not_matching_query"
                continue
            final = Path(output_dir) / f"visual-stock-{beat['id']}.mp4"
            Path(found_clips[0]).replace(final)
            row["local_file"] = final.name
            row["stock_query_variant_used"] = "primary" if attempt == 0 else "approved_alternate"
            return (final, row), ""
        return None, reason

    def _try_pinned_sequence(self, beat: Mapping[str, Any], asset: Path, output_dir: Path,
                             fmt: str, seconds: float, index: int):
        """Join the editor's three VIDEO links in order, then approved art for any gap.

        The opt-in mode never reinterprets legacy prioritized alternatives. Failed
        candidates are skipped rather than replacing the whole scene with still art.
        The output has no audio: the existing voice-owned timeline remains authoritative.
        """
        from .media import _run, probe_duration

        urls = beat["owner_stock"]["urls"]
        remaining = max(0.0, float(seconds))
        pieces: list[tuple[Path, float, dict[str, Any]]] = []
        rejections: list[dict[str, Any]] = []
        for rank, url in enumerate(urls, 1):
            if remaining <= 0.08:
                break
            admitted, reason = self._try_pinned_one(
                beat, asset, output_dir, fmt, remaining, index, str(url)
            )
            if admitted is None:
                rejections.append({"rank": rank, "reason": str(reason)[:120]})
                continue
            clip, row = admitted
            if row.get("media_kind") != "video":
                clip.unlink(missing_ok=True)
                rejections.append({"rank": rank, "reason": "sequence_requires_video"})
                continue
            available = probe_duration(clip)
            used = min(remaining, available)
            # PINNED_MIN_SECONDS checks the *source*, not the final trimmed
            # fraction. A verified 2-second clip may fill the last 0.6 seconds.
            if used <= 0.08:
                clip.unlink(missing_ok=True)
                rejections.append({"rank": rank, "reason": "sequence_no_remaining_slot"})
                continue
            part = Path(output_dir) / f"sequence-{beat['id']}-{rank}.mp4"
            clip.replace(part)
            pieces.append((part, used, row))
            remaining = max(0.0, remaining - used)

        if not pieces:
            return None, "pinned_sequence_exhausted:" + ",".join(
                item["reason"] for item in rejections
            )

        has_art_tail = remaining > 0.08
        if has_art_tail:
            tail = Path(output_dir) / f"sequence-{beat['id']}-owner.mp4"
            try:
                make_motion_clip(
                    asset, tail, seconds=remaining + 0.5, direction=index,
                    portrait=fmt == "short",
                    motion_kind=beat["owner_motion"], respect_hold=True,
                )
                pieces.append((tail, remaining, {"source_actual": PROVIDER}))
            except Exception as exc:
                for part, _, _ in pieces:
                    part.unlink(missing_ok=True)
                return None, f"pinned_sequence_tail_failed:{type(exc).__name__}"

        final = Path(output_dir) / f"visual-stock-{beat['id']}.mp4"
        width, height = (720, 1280) if fmt == "short" else (CLIP_WIDTH, CLIP_HEIGHT)
        # Normalize frame sizes, clocks and pixel formats before lossless-timing
        # concat. No reverse, loop, repeat, audio edits or time stretching.
        inputs: list[str] = []
        filters: list[str] = []
        for j, (part, duration, _) in enumerate(pieces):
            inputs.extend(["-i", str(part)])
            filters.append(
                f"[{j}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},fps=30,setsar=1,format=yuv420p,"
                f"trim=duration={duration:.3f},setpts=PTS-STARTPTS[v{j}]"
            )
        graph = ";".join(filters) + ";" + "".join(
            f"[v{j}]" for j in range(len(pieces))
        ) + f"concat=n={len(pieces)}:v=1:a=0[out]"
        try:
            _run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
                 "-filter_complex", graph, "-map", "[out]", "-an",
                 "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
                 "-pix_fmt", "yuv420p", str(final)],
                timeout=600,
            )
        except Exception as exc:
            final.unlink(missing_ok=True)
            return None, f"pinned_sequence_join_failed:{type(exc).__name__}"
        finally:
            for part, _, _ in pieces:
                part.unlink(missing_ok=True)

        videos = [(duration, row) for _, duration, row in pieces
                  if row.get("source_actual") == "stock_motion"]
        row = dict(videos[0][1])
        row.update(
            local_file=final.name,
            pinned_sequence=True,
            pinned_sequence_requested=len(urls),
            pinned_sequence_used=len(videos),
            pinned_sequence_sources=[
                {"source_url": segment["source_url"],
                 "provider": segment["provider"],
                 "asset_id": segment["asset_id"],
                 "seconds": round(duration, 2)}
                for duration, segment in videos
            ],
            pinned_sequence_rejections=rejections,
            pinned_source_seconds=round(sum(duration for duration, _ in videos), 2),
            pinned_sequence_slot_seconds=round(float(seconds), 2),
            pinned_sequence_motion_share=round(
                sum(duration for duration, _ in videos) / max(0.001, float(seconds)), 3
            ),
            pinned_continues_with_image=has_art_tail,
            # Composite source requires editorial review of EVERY inserted shot.
            sequence_visual_review_required=True,
        )
        return (final, row), ""

    def _try_pinned(self, beat: Mapping[str, Any], asset: Path, output_dir: Path, fmt: str,
                    seconds: float, index: int, template: str = ""):
        """Try up to three editor-approved stock links in priority order, then the approved image."""
        stock = beat["owner_stock"]
        if stock.get("mode") == "sequence":
            return self._try_pinned_sequence(beat, asset, output_dir, fmt, seconds, index)
        urls = stock.get("urls") or [stock.get("url")]
        reasons = []
        for rank, url in enumerate(urls, start=1):
            admitted, reason = self._try_pinned_one(beat, asset, output_dir, fmt, seconds, index, str(url))
            if admitted is not None:
                clip, row = admitted
                row["pinned_choice_rank"] = rank
                row["pinned_alternates_total"] = len(urls)
                row["pinned_prior_rejections"] = reasons
                return (clip, row), ""
            reasons.append({"rank": rank, "reason": str(reason)[:120]})
        # Three links that were each usable but each shorter than the slot are
        # chained as a sequence instead of dropping to still art. Only for short
        # Nidaa / film (the formats that accept stock.mode="sequence"); blocked or
        # missing links never trigger it and the approved image stays the fallback.
        if (len(urls) == 3 and fmt in {"short", "film"} and template != "ala_mahl"
                and any(r["reason"] == "pinned_clip_too_short_for_scene" for r in reasons)
                and all(r["reason"] in {"pinned_clip_too_short_for_scene", "pinned_clip_too_short"}
                        for r in reasons)):
            chained, why = self._try_pinned_sequence(
                {**beat, "owner_stock": {**stock, "urls": list(urls), "mode": "sequence"}},
                asset, output_dir, fmt, seconds, index)
            if chained is not None:
                chained[1]["pinned_sequence_fallback_from_alternatives"] = True
                chained[1]["pinned_prior_rejections"] = reasons
                return chained, ""
            reasons.append({"rank": 0, "reason": str(why)[:120]})
        if len(reasons) == 1:
            return None, reasons[0]["reason"]  # preserve legacy single-URL diagnostics
        return None, "pinned_alternatives_exhausted:" + ",".join(row["reason"] for row in reasons)

    def _try_pinned_one(self, beat: Mapping[str, Any], asset: Path, output_dir: Path, fmt: str,
                        seconds: float, index: int, url: str):
        """The exact Pexels/Pixabay video or photo the editor linked, or (None, reason) -> approved image.

        Fetched by id, no search. Three-link packages retry a video that cannot
        cover the scene; legacy single-URL packages may continue with approved art.
        Pictures run as an owner image would. The normal
        picture review still judges the result afterwards (objective risks only), so this never
        becomes an unchecked path; any failure keeps the approved image.
        """
        from . import pinned_stock
        from .media import _download_media, _run, probe_duration

        portrait = fmt == "short"
        try:
            cand = pinned_stock.fetch_candidate(url, portrait=portrait)
        except pinned_stock.PinnedStockError as exc:
            return None, str(exc)
        work = Path(output_dir) / f"pinned-{beat['id']}"
        work.mkdir(parents=True, exist_ok=True)
        final = Path(output_dir) / f"visual-stock-{beat['id']}.mp4"
        photo = cand["media_kind"] == "photo"
        source = work / ("source.jpg" if photo else "source.mp4")
        preflight = getattr(self._inner, "media_preflight", None)
        extra: dict[str, Any] = {}
        try:
            _download_media(cand["download_url"], source)
            if photo:
                make_motion_clip(source, final, seconds=seconds, direction=index, portrait=portrait,
                                 motion_kind=beat["owner_motion"], respect_hold=True)
                if preflight is not None and preflight(final) is not None:
                    final.unlink(missing_ok=True)
                    return None, "pinned_security_blocked"
            else:
                if preflight is not None and preflight(source) is not None:
                    return None, "pinned_security_blocked"
                transform = getattr(self._inner, "media_transform", None)
                clip = Path(transform(source)) if transform is not None else source
                length = probe_duration(clip)
                if length < PINNED_MIN_SECONDS:
                    return None, "pinned_clip_too_short"
                if clip_is_grayscale(clip, length=length):
                    return None, "pinned_clip_grayscale"
                # For three-link scene packages an admitted stock choice must cover
                # the entire estimated slot. Do not quietly append the owner
                # fallback photo while a stock URL was successfully selected.
                # Retry the next link instead. Single-URL legacy packs retain
                # the original video-then-owner-image continuation behavior.
                if (len(beat.get("owner_stock", {}).get("urls", [])) == 3
                        and beat.get("owner_stock", {}).get("mode") != "sequence"
                        and length + 0.08 < seconds):
                    return None, "pinned_clip_too_short_for_scene"
                if length + 0.08 >= seconds or beat.get("owner_stock", {}).get("mode") == "sequence":
                    clip.replace(final)
                else:
                    fade = PINNED_FADE_SECONDS
                    tail = work / "tail.mp4"
                    make_motion_clip(asset, tail, seconds=seconds - length + fade + 0.5, direction=index,
                                     portrait=portrait, motion_kind=beat["owner_motion"], respect_hold=True)
                    width, height = (720, 1280) if portrait else (CLIP_WIDTH, CLIP_HEIGHT)
                    graph = (f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
                             "fps=30,setsar=1,format=yuv420p[a];[1:v]fps=30,setsar=1,format=yuv420p[b];"
                             f"[a][b]xfade=transition=fade:duration={fade}:offset={length - fade:.3f}[v]")
                    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip), "-i", str(tail),
                          "-filter_complex", graph, "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "ultrafast",
                          "-crf", "22", "-pix_fmt", "yuv420p", str(final)], timeout=300)
                    extra = {"pinned_continues_with_image": True, "pinned_source_seconds": round(length, 2)}
        except Exception as exc:  # the approved image covers any failure
            final.unlink(missing_ok=True)
            return None, f"pinned_prepare_failed:{type(exc).__name__}"
        row = {
            "provider": cand["provider"], "asset_id": cand["asset_id"], "source_url": cand["source_url"],
            "creator": cand["creator"], "creator_url": cand["creator_url"], "query": "pinned_url",
            "local_file": final.name, "media_kind": cand["media_kind"], "attribution_required": False,
            "source_actual": "stock_still" if photo else "stock_motion", "stock_pinned": True,
            "viewer_intent": str(beat.get("viewer_intent") or ""), "meaning_target": str(beat.get("meaning_target") or ""),
            "shot_intent": str(beat.get("shot_intent") or ""), "writer_anchor_ar": str(beat.get("writer_anchor_ar") or ""),
            "display_text_ar": "", "role": str(beat.get("role") or ""), "shot_role": str(beat.get("shot_role") or ""),
            **extra,
        }
        return (final, row), ""

    def _board_clip(self, beat: Mapping[str, Any], output_dir: Path, fmt: str, section_seconds: float):
        """The explainer board for this beat from an estimated slot, or (None, reason) -> approved image.

        Built here from the estimate so the clip is always a complete board; render_video rebuilds it
        to the measured slot when the voice timeline is known. Any failure keeps the approved image.
        """
        from . import board_scene

        info = beat["owner_board"]
        try:
            scene_words = max(1, len(str(info["text"]).split()))
            share = scene_words / max(scene_words, int(info.get("section_words") or scene_words))
            seconds = max(8.0, float(section_seconds) * share)
            times = board_scene.reveal_times(str(info["text"]), info["spec"]["items"], seconds, compress=0.7)
            clip = board_scene.render_board_clip(info["spec"], self._directory, output_dir / f"visual-owner-{beat['id']}.mp4",
                                                 seconds=seconds, times=times, fmt=fmt)
            return clip, {"board_rendered": True, "owner_board_root": str(self._directory)}
        except Exception as exc:  # the approved image is the fallback; production never stops for a board
            return None, {"board_fallback_reason": f"{type(exc).__name__}:{str(exc)[:160]}"}

    def _acquire_ready(self, plan, output_dir, fmt, estimates):
        """One existing motion clip per approved beat; never fall back to stock."""
        from .production_package import validate_manifest

        doc = json.loads((self._directory / "production.json").read_text(encoding="utf-8"))
        validate_manifest(doc, {str(p.relative_to(self._directory)) for p in self._directory.rglob("*") if p.is_file()})
        beats = plan["visual_story"]["beats"]
        clips, rights = [], []
        # Estimate each podcast/film shot from the actual text span. Using the full
        # section length for EVERY shot made multi-shot episodes excessively costly:
        # a 36-scene film asked every scene to build a whole section's worth of video.
        podcast_seconds: dict[str, float] = {}
        if fmt in {"podcast", "film"}:
            podcast_seconds = _span_beat_seconds(doc, beats, estimates)
        output_dir.mkdir(parents=True, exist_ok=True)
        for index, beat in enumerate(beats):
            asset = self._directory / beat["owner_asset"]
            if not asset.is_file():
                raise RuntimeError("approved_package_image_missing:" + beat["owner_asset"])
            # The shared renderer trims these sources to its measured audio slots.
            sections = {beat["section_id"]}
            if doc["template"] == "ala_mahl":
                sections = {b["section_id"] for b in beats if b["owner_asset"] == beat["owner_asset"]}
            stock_note: dict[str, Any] = {}
            seconds = podcast_seconds.get(beat["id"], sum(float((estimates or {}).get(sid) or 60) for sid in sections) + 1)
            if beat.get("owner_stock"):
                pinned = bool(beat["owner_stock"].get("url") or beat["owner_stock"].get("urls"))
                if pinned:
                    admitted, reason = self._try_pinned(beat, asset, output_dir, fmt, seconds, index,
                                                            template=str(doc.get("template") or ""))
                else:
                    admitted, reason = self._try_stock(beat, plan, output_dir, fmt)
                if admitted is not None:
                    clips.append(admitted[0])
                    rights.append({**admitted[1], "beat_id": beat["id"],
                                   "section_id": beat["section_id"],
                                   # Stock changes picture source, never the owner's
                                   # approved transition at this exact voice anchor.
                                   "owner_transition": beat["owner_transition"],
                                   "stock_requested": True,
                                   **({"stock_pinned_requested": True} if pinned else {}),
                                   "approved_fallback_asset": beat["owner_asset"], "owner_approved": False})
                    continue
                stock_note = {"stock_requested": True, "stock_fallback_reason": reason,
                              **({"stock_pinned_requested": True} if pinned else {})}
            board_note: dict[str, Any] = {}
            clip = None
            if beat.get("owner_board"):
                clip, board_note = self._board_clip(beat, output_dir, fmt, seconds)
            if clip is None:
                clip = make_motion_clip(asset, output_dir / f"visual-owner-{beat['id']}.mp4", seconds=seconds,
                                        direction=index, portrait=fmt == "short", motion_kind=beat["owner_motion"],
                                        respect_hold=True)
            clips.append(clip)
            rights.append({**dict(beat), "beat_id": beat["id"], "provider": PROVIDER,
                           "asset_id": beat["owner_asset"], "local_file": clip.name, "source_actual": PROVIDER,
                           "owner_approved": True, "creator": "owner supplied", "attribution_required": False,
                           **stock_note, **board_note})
        if not clips:
            raise RuntimeError("approved_package_has_no_images")
        look = grade_profile(str(doc.get("template") or ""), str(doc.get("format") or fmt))
        for clip, row in zip(clips, rights):
            if row.get("board_rendered"):
                continue  # explanatory boards keep their own colours
            row["grade"] = grade_clip(clip, look)
        return clips, rights


def _span_beat_seconds(doc: Mapping[str, Any], beats: list[Mapping[str, Any]],
                       estimates: Mapping[str, Any] | None) -> dict[str, float]:
    """Estimated seconds for each beat from its share of the section's spoken words.

    The renderer still trims to the measured voice slots; this only stops every beat
    from preparing a clip as long as its whole section. Any beat whose anchor cannot be
    located keeps the legacy whole-section estimate (the caller's default).
    """
    from .production_package import section_text, spoken_text

    seconds: dict[str, float] = {}
    for section in doc["script"]["sections"]:
        sid = section["id"]
        group = [b for b in beats if b["section_id"] == sid]
        if not group:
            continue
        narration = spoken_text(section_text(section))
        try:
            starts = [narration.index(str(b["writer_anchor_ar"])) for b in group]
        except ValueError:
            continue
        ends = starts[1:] + [len(narration)]
        weights = [max(1, len(narration[a:b].split())) for a, b in zip(starts, ends)]
        total = sum(weights)
        duration = float((estimates or {}).get(sid) or 60)
        for beat, weight in zip(group, weights):
            seconds[beat["id"]] = max(8.0, duration * weight / total * 1.12 + 2)
    return seconds


def grade_profile(template: str, fmt: str) -> str:
    return "ala_mahl" if template == "ala_mahl" else (
        "podcast" if fmt == "podcast" else ("short" if fmt == "short" else "film")
    )


def grade_clip(clip: Path, profile: str) -> str:
    """Re-encode one scene clip in place with the format's look. Fail-soft: the clip stays as it was."""
    clip = Path(clip)
    if profile == "podcast":
        # No film-style colour wash on natural photographic podcast coverage.
        return "podcast_natural_source_ungraded"
    graded = clip.with_name(clip.stem + ".graded.mp4")
    try:
        from .media import _run, probe_duration

        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip), "-vf",
              GRADE_PROFILES[profile] + ",format=yuv420p", "-an", "-c:v", "libx264", "-preset", "ultrafast",
              "-crf", "20", "-pix_fmt", "yuv420p", str(graded)], timeout=600)
        if abs(probe_duration(graded) - probe_duration(clip)) > 0.2:
            raise RuntimeError("duration_changed")
        graded.replace(clip)
        return profile
    except Exception as exc:
        graded.unlink(missing_ok=True)
        return f"unavailable:{type(exc).__name__}"


def apply_owner_derivative_cover(cover_path: Path, report: dict[str, Any], index: int = 0) -> dict[str, Any]:
    """The cover the editor designed for the derived Short, if the approved package carries one.

    Replaces the frame-crop cover Cover Lite just made. Fail-soft: anything wrong keeps that cover.
    """
    try:
        directory = art_dir()
        if not directory or not (directory / "production.json").is_file():
            return report
        doc = json.loads((directory / "production.json").read_text(encoding="utf-8"))
        if index <= 0:
            owner = doc.get("derivative") or {}
        else:  # extra Short number `index` (1-based) of the same episode
            extras = doc.get("extra_derivatives") or []
            owner = extras[index - 1] if index <= len(extras) and isinstance(extras[index - 1], dict) else {}
        cover = owner.get("cover")
        if not isinstance(cover, dict) or not cover.get("asset"):
            return report
        normalize_image(directory / cover["asset"], Path(cover_path), (1080, 1920))
        return {**report, "status": "pass", "source": "owner_derivative_cover_image",
                "cover_text": str(cover.get("text") or ""), "provider_calls_added": 0}
    except Exception as exc:  # fail-soft: the generated cover stays
        return {**report, "owner_derivative_cover": f"unavailable:{type(exc).__name__}:{str(exc)[:120]}"}


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
            normalize_image(source, Path(cover_path), (1080, 1920) if doc["format"] == "short" else COVER_SIZE)
            return {**report, "status": "pass", "source": "owner_cover_image", "provider_calls_added": 0}
    source = cover_image(directory) if directory else None
    if source is None:
        return report
    normalize_image(source, Path(cover_path), COVER_SIZE)
    return {**report, "status": "pass", "source": "owner_cover_image", "provider_calls_added": 0}
