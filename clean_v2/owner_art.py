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
        workdir = Path(output_dir) / f"stock-{beat['id']}"
        try:
            found_clips, found_rights = self._inner.acquire(sub_plan, workdir, fmt, 1, None)
        except Exception as exc:  # the approved image covers any stock failure
            return None, f"stock_error:{type(exc).__name__}"
        if len(found_clips) != 1 or len(found_rights) != 1:
            return None, "stock_no_single_clip"
        row = dict(found_rights[0])
        if str(row.get("source_actual") or "") not in {"stock_motion", "stock_still"}:
            return None, "no_admitted_stock_candidate"
        # The editor wrote three descriptions of the same wanted picture: the main query, the broader
        # alternate and `must_show`. Provider titles rarely repeat one phrase word for word, so a clip
        # that fits any of the three is the clip the editor asked for. The same strict fit check runs
        # on each; nothing is loosened inside it.
        wanted = [query, alternate, str(stock.get("must_show") or "")]
        if not any(text.strip() and _stock_review_semantic_fit(text, row) for text in wanted):
            return None, "stock_metadata_not_matching_query"
        # Every per-beat search names its clip visual-01.mp4: give it a unique name next to the
        # other clips so rights rows and the renderer can never confuse two beats.
        final = Path(output_dir) / f"visual-stock-{beat['id']}.mp4"
        Path(found_clips[0]).replace(final)
        row["local_file"] = final.name
        return (final, row), ""

    def _try_pinned(self, beat: Mapping[str, Any], asset: Path, output_dir: Path, fmt: str,
                    seconds: float, index: int):
        """The exact Pexels/Pixabay video or photo the editor linked, or (None, reason) -> approved image.

        Fetched by id, no search. A clip shorter than the scene plays first and the scene's approved
        image continues it to the end of the slot. Pictures run as an owner image would. The normal
        picture review still judges the result afterwards (objective risks only), so this never
        becomes an unchecked path; any failure keeps the approved image.
        """
        from . import pinned_stock
        from .media import _download_media, _run, probe_duration

        portrait = fmt == "short"
        try:
            cand = pinned_stock.fetch_candidate(str(beat["owner_stock"]["url"]), portrait=portrait)
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
                if length + 0.08 >= seconds:
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
            seconds = sum(float((estimates or {}).get(sid) or 60) for sid in sections) + 1
            if beat.get("owner_stock"):
                pinned = bool(beat["owner_stock"].get("url"))
                if pinned:
                    admitted, reason = self._try_pinned(beat, asset, output_dir, fmt, seconds, index)
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
        return clips, rights


def apply_owner_derivative_cover(cover_path: Path, report: dict[str, Any]) -> dict[str, Any]:
    """The cover the editor designed for the derived Short, if the approved package carries one.

    Replaces the frame-crop cover Cover Lite just made. Fail-soft: anything wrong keeps that cover.
    """
    try:
        directory = art_dir()
        if not directory or not (directory / "production.json").is_file():
            return report
        doc = json.loads((directory / "production.json").read_text(encoding="utf-8"))
        cover = (doc.get("derivative") or {}).get("cover")
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
            normalize_image(source, Path(cover_path), (720, 1280) if doc["format"] == "short" else COVER_SIZE)
            return {**report, "status": "pass", "source": "owner_cover_image", "provider_calls_added": 0}
    source = cover_image(directory) if directory else None
    if source is None:
        return report
    normalize_image(source, Path(cover_path), COVER_SIZE)
    return {**report, "status": "pass", "source": "owner_cover_image", "provider_calls_added": 0}
