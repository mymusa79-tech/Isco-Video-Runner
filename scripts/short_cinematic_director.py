from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.ai_budget import BudgetLedger, Capability, Priority, TaskSpec
from isco_video_agent.cinematic_m8_color_kernel import normalize_to_bt709_sdr, report_dict
from isco_video_agent.cinematic_sfx import SfxEvent, materialize_sfx_library, mix_sfx_into_narration
from isco_video_agent.config import env, secret
from isco_video_agent.media.ffmpeg import concat_video, duration, make_review_preview, prepare_clip
from isco_video_agent.providers import pixabay as pixabay_provider
from isco_video_agent.providers.gemini import audit_video_preview
from isco_video_agent.providers.pexels import (
    best_file,
    download as pexels_download,
    review_file,
    search_videos as pexels_search_videos,
)
from isco_video_agent.rights import VISUAL_RISK_FLAGS, _visual_rights_entry
from isco_video_agent.short_timed_text import render_progressive_text
from isco_video_agent.stock_media_preflight import inspect_stock_media, local_preflight_block
from isco_video_agent.visual_selection import VisualCandidateCache, select_with_recovery

from scripts.opening_feasibility_guard import _stable_intent_audit


PROFILE = "short_cinematic_director_v1"
MAX_SHORT_SHOTS = 4
MIN_SHORT_SHOTS = 2
# One cloud Vision verdict on the primary retrieval and at most one on the deterministic
# alternate retrieval. This keeps a four-beat Short to <=6 *additional* Vision calls in
# the absolute worst case (three added beats x two reviews), while local preflight may
# reject more media cheaply without consuming the semantic review allowance.
MAX_VISION_REVIEWS_PER_ATTEMPT = 1
MAX_VISION_REVIEWS_PER_BEAT = 2
MAX_TOTAL_INSPECTIONS_PER_BEAT = 6

_TEMPLATE_QUERY_MODIFIERS: dict[str, tuple[str, ...]] = {
    "why_reframe": (
        "immediate visual tension",
        "contrasting perspective realistic",
        "person changing direction subtle action",
        "hopeful practical movement",
    ),
    "inner_dialogue": (
        "intimate reflective close detail",
        "hesitation hands subtle tension",
        "perspective shift realistic human action",
        "person standing moving forward hopeful",
    ),
    "micro_story": (
        "immediate establishing action",
        "concrete human action detail",
        "clear turning point realistic",
        "forward movement meaningful payoff",
    ),
    "quote_reflection": (
        "calm symbolic visual detail",
        "quiet reflective pause",
        "subtle perspective shift",
        "gentle hopeful release",
    ),
}

_TEMPLATE_ALT_MODIFIERS: dict[str, tuple[str, ...]] = {
    "why_reframe": ("close detail", "opposite angle", "decisive action", "small practical step"),
    "inner_dialogue": ("quiet closeup", "restless hands", "breath reset", "walking forward"),
    "micro_story": ("scene detail", "action closeup", "change moment", "meaningful resolution"),
    "quote_reflection": ("minimal detail", "stillness", "soft movement", "open hopeful space"),
}

_SHORT_SFX_BY_TEMPLATE = {
    "why_reframe": "soft_hit_02",
    "inner_dialogue": "low_bloom_02",
    "micro_story": "air_whoosh_02",
}


class ShortCinematicError(RuntimeError):
    pass


def _read_json(path: Path, expected: type) -> Any:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ShortCinematicError(f"Short cinematic director requires valid {path.name}") from exc
    if not isinstance(data, expected):
        raise ShortCinematicError(f"Short cinematic director expected {path.name} to be {expected.__name__}")
    return data


def _clean(value: object, limit: int = 500) -> str:
    return " ".join(str(value or "").strip().split())[:limit]


def _event_duration(event: dict[str, Any]) -> float:
    try:
        start = float(event.get("start") or 0.0)
        end = float(event.get("end") or 0.0)
    except (TypeError, ValueError) as exc:
        raise ShortCinematicError("Short cinematic event has invalid timing") from exc
    if end <= start:
        raise ShortCinematicError("Short cinematic event has non-positive timing")
    return end - start


def required_shot_count(events: list[dict[str, Any]], duration_seconds: float) -> int:
    """One semantic beat per shot, bounded to a professional 2-4 shot Short."""
    del duration_seconds
    count = len(events)
    if count < MIN_SHORT_SHOTS:
        raise ShortCinematicError("Short cinematic director requires at least two semantic beats")
    return min(MAX_SHORT_SHOTS, count)


def beat_queries(base_query: str, template: str, index: int) -> tuple[str, str]:
    if template not in _TEMPLATE_QUERY_MODIFIERS:
        raise ShortCinematicError("Short cinematic director received unsupported template")
    modifiers = _TEMPLATE_QUERY_MODIFIERS[template]
    alternates = _TEMPLATE_ALT_MODIFIERS[template]
    slot = min(max(0, int(index)), len(modifiers) - 1)
    base = _clean(base_query, 200)
    if not base:
        raise ShortCinematicError("Short cinematic director requires an English visual query")
    return (
        _clean(f"{base} {modifiers[slot]} portrait vertical realistic cinematic", 260),
        _clean(f"{base} {alternates[slot]} portrait vertical realistic cinematic", 260),
    )


def _provider_asset_id(credit: dict[str, Any]) -> tuple[str, object]:
    provider = _clean(credit.get("provider"), 30).lower()
    asset_id = credit.get("asset_id")
    if asset_id in (None, ""):
        asset_id = credit.get("pexels_id") if provider == "pexels" else credit.get("pixabay_id")
    return provider, asset_id


def _trim_video(src: Path, dest: Path, start: float, seconds: float) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0.0, start):.3f}", "-i", str(src), "-t", f"{seconds:.3f}",
            "-an", "-vf", "fps=30,setsar=1,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(dest),
        ],
        check=True,
    )
    if not dest.is_file() or dest.stat().st_size <= 1024:
        raise ShortCinematicError("Short cinematic trim did not produce a usable shot")
    return dest


def _remux_video_with_existing_audio(video: Path, audio_source_video: Path, dest: Path) -> Path:
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video), "-i", str(audio_source_video),
            "-map", "0:v:0", "-map", "1:a?", "-c:v", "copy", "-c:a", "copy",
            "-shortest", "-movflags", "+faststart", str(dest),
        ],
        check=True,
    )
    if not dest.is_file() or dest.stat().st_size <= 1024:
        raise ShortCinematicError("Short cinematic remux did not produce a usable master")
    return dest


def _append_rights(root: Path, credits: list[dict[str, Any]], new_credits: list[dict[str, Any]], metadata: dict[str, Any]) -> None:
    credits_path = root / "credits.json"
    rights_path = root / "rights-manifest.json"
    rights = _read_json(rights_path, dict)
    existing_visuals = rights.get("visuals")
    if not isinstance(existing_visuals, list):
        raise ShortCinematicError("Short cinematic director requires visual rights records")

    seen = {
        (str(item.get("provider") or "").lower(), str(item.get("asset_id") or ""))
        for item in existing_visuals
        if isinstance(item, dict)
    }
    for credit in new_credits:
        entry = _visual_rights_entry(credit)
        key = (str(entry.get("provider") or "").lower(), str(entry.get("asset_id") or ""))
        if key in seen:
            raise ShortCinematicError("Short cinematic director selected a duplicate rights asset")
        existing_visuals.append(entry)
        seen.add(key)

    rights["short_cinematic_v1"] = metadata
    rights_path.write_text(json.dumps(rights, ensure_ascii=False, indent=2), encoding="utf-8")
    credits_path.write_text(json.dumps([*credits, *new_credits], ensure_ascii=False, indent=2), encoding="utf-8")


def _rights_flags(audit: dict[str, Any]) -> dict[str, bool]:
    aliases = {
        "prominent_logo_or_brand": "prominent_logo_or_brand",
        "sensitive_trait_implication_risk": "sensitive_trait_implication_risk",
        "cultural_conflict": "cultural_conflict",
        "advertiser_conflict": "advertiser_conflict",
    }
    flags = {name: False for name in VISUAL_RISK_FLAGS}
    for target, source in aliases.items():
        flags[target] = bool(audit.get(source))
    return flags


def _prepare_m8_clip(raw: Path, dest: Path, seconds: float) -> Path:
    temp = dest.with_name(dest.stem + "-bt709-sdr.mp4")
    try:
        report = normalize_to_bt709_sdr(raw, temp)
        prepared = prepare_clip(temp, dest, seconds, portrait=True, fps=30)
        payload = {
            **report_dict(report),
            "status": "applied",
            "production_stage": "short_cinematic_asset_normalization_before_creative_grade",
            "source": raw.name,
            "final_clip": dest.name,
            "creative_grade_authority": "media.color.build_color_filter_after_m8",
        }
        dest.with_suffix(".m8.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return prepared
    finally:
        temp.unlink(missing_ok=True)


def _search_assets(query: str, *, pexels_key: str, pixabay_key: str) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    if pexels_key:
        rows = pexels_search_videos(pexels_key, query, orientation="portrait", per_page=12)
        if rows:
            found["pexels"] = rows
    if pixabay_key:
        rows = pixabay_provider.search_videos(pixabay_key, query, orientation="portrait", per_page=12)
        if rows:
            found["pixabay"] = rows
    return found


def _download_for_provider(provider: str, url: str, dest: Path) -> Path:
    if provider == "pexels":
        return pexels_download(url, dest)
    if provider == "pixabay":
        return pixabay_provider.download(url, dest)
    raise ShortCinematicError(f"Unsupported Short cinematic provider: {provider}")


def upgrade_short_cinematic(
    output_dir: Path,
    control_request: dict[str, Any],
    pre_gold: dict[str, Any],
    *,
    ledger: BudgetLedger,
) -> dict[str, Any]:
    """Replace single-asset Short compensation with a beat-native audited multi-shot master.

    Called after Short Voice V2 has produced the voiced master and before authoritative
    Final Master QC / Gold. Audio is preserved byte-for-byte until the optional local
    procedural SFX pass. Every additional stock asset receives local preflight, bounded
    cloud Visual QA, M8 BT.709 normalization, creative grade and explicit rights provenance.
    """
    root = Path(output_dir)
    if control_request.get("kind") != "short":
        return pre_gold
    scope = _clean(control_request.get("approval_scope"), 40)
    if scope != "short_only":
        # Source-derived Shorts must remain bound to the approved long episode's exact
        # sections/assets; do not silently introduce unrelated stock here.
        return pre_gold

    template = _clean(pre_gold.get("short_template"), 40)
    events = [item for item in list(pre_gold.get("timed_text_events") or []) if isinstance(item, dict)]
    if template not in _TEMPLATE_QUERY_MODIFIERS:
        raise ShortCinematicError("Short cinematic director requires a certified Short template")
    quality = _read_json(root / "quality-final.json", dict)
    try:
        total_seconds = float(quality.get("duration_seconds") or quality.get("video_stream_duration") or 0.0)
    except (TypeError, ValueError) as exc:
        raise ShortCinematicError("Short cinematic director cannot resolve duration") from exc
    if not 7.0 <= total_seconds <= 25.0:
        raise ShortCinematicError("Short cinematic director received duration outside Short hard limits")
    shot_count = required_shot_count(events, total_seconds)
    events = events[:shot_count]

    plan = _read_json(root / "plan.json", dict)
    sections = plan.get("sections") if isinstance(plan.get("sections"), list) else []
    first_section = sections[0] if sections and isinstance(sections[0], dict) else {}
    base_query = _clean(first_section.get("visual_query"), 260)
    if not base_query:
        raise ShortCinematicError("Short cinematic director requires the approved visual query")

    picture = root / "picture.mp4"
    final_path = root / "final.mp4"
    if not picture.is_file() or not final_path.is_file():
        raise ShortCinematicError("Short cinematic director requires picture.mp4 and voiced final.mp4")
    credits = _read_json(root / "credits.json", list)
    visual_audits = _read_json(root / "visual-audit.json", list)
    if not credits:
        raise ShortCinematicError("Short cinematic director requires the core audited visual credit")
    original_credit = next((item for item in credits if isinstance(item, dict)), None)
    if original_credit is None:
        raise ShortCinematicError("Short cinematic director cannot resolve the core visual credit")

    gemini = secret("GEMINI_API_KEY")
    pexels_key = secret("PEXELS_API_KEY")
    pixabay_key = secret("PIXABAY_API_KEY")
    if not gemini:
        raise ShortCinematicError("Short cinematic Visual QA requires Gemini")
    if not (pexels_key or pixabay_key):
        raise ShortCinematicError("Short cinematic director requires Pexels or Pixabay")
    content_model = env("GEMINI_CONTENT_MODEL", "gemini-3.7-flash") or "gemini-3.7-flash"

    # Preserve the same cross-run visual freshness exclusion used by the canonical
    # selector. The already-selected core Moment asset is then marked in-run so no
    # later beat can silently reuse it even under a different retrieval query.
    cache = VisualCandidateCache()
    original_provider, original_asset_id = _provider_asset_id(original_credit)
    if not original_provider or original_asset_id in (None, ""):
        raise ShortCinematicError("Short cinematic core credit lacks provider-qualified asset identity")
    cache.mark_selected(original_provider, original_asset_id)

    work = root / "short-cinematic-v1"
    review_root = work / "review"
    raw_root = work / "raw"
    prepared_root = work / "prepared"
    for directory in (review_root, raw_root, prepared_root):
        directory.mkdir(parents=True, exist_ok=True)

    prepared: list[Path] = []
    timeline_shots: list[dict[str, Any]] = []
    new_credits: list[dict[str, Any]] = []

    first = events[0]
    first_seconds = _event_duration(first)
    first_shot = _trim_video(picture, prepared_root / "shot-01-core.mp4", 0.0, first_seconds)
    prepared.append(first_shot)
    timeline_shots.append(
        {
            "shot_id": "short-shot-01",
            "beat_id": "b01",
            "role": _clean(first.get("role"), 30) or "hook",
            "start_seconds": round(float(first.get("start") or 0.0), 3),
            "end_seconds": round(float(first.get("end") or first_seconds), 3),
            "provider": original_provider,
            "asset_id": original_asset_id,
            "query": original_credit.get("query") or base_query,
            "intended_visual": base_query,
            "source": "core_moment_audited_asset",
            "transition_in": "start",
        }
    )

    for beat_index, event in enumerate(events[1:], 2):
        shot_seconds = _event_duration(event)
        primary_query, alternate_query = beat_queries(base_query, template, beat_index - 1)
        audit_count = {"n": 0}
        inspection_count = {"n": 0}

        def audit_fn(*, provider: str, candidate: dict, narration_context: str, intended_visual: str) -> dict:
            inspection_count["n"] += 1
            inspection_index = inspection_count["n"]
            downloadable = review_file(candidate, portrait=True)
            if not downloadable:
                return local_preflight_block(
                    "no_downloadable_review_file",
                    "Short cinematic candidate has no portrait review file",
                    candidate_inspection_index=inspection_index,
                )
            review_src = _download_for_provider(
                provider,
                downloadable["link"],
                review_root / f"b{beat_index:02d}-{provider}-i{inspection_index:02d}-source.mp4",
            )
            preview = make_review_preview(
                review_src,
                review_root / f"b{beat_index:02d}-{provider}-i{inspection_index:02d}-preview.mp4",
                portrait=True,
            )
            local = inspect_stock_media(preview)
            if local is not None:
                local["candidate_inspection_index"] = inspection_index
                return local

            audit_count["n"] += 1
            review_index = audit_count["n"]
            priority = Priority.P0 if review_index == 1 else Priority.P1
            result = orchestrator._ledger_call_status(
                ledger,
                TaskSpec(
                    task_id=f"SHORT_VISUAL_AUDIT_B{beat_index:02d}_C{review_index:02d}",
                    kind="SHORT_VISUAL_AUDIT",
                    priority=priority,
                    capability=Capability.VISION,
                    max_provider_attempts=1,
                    schema_repair_allowed=False,
                    local_fallback=False,
                    semantic_block_is_final=False,
                ),
                "gemini",
                content_model,
                audit_video_preview,
                gemini,
                preview,
                narration_context=narration_context,
                intended_visual=intended_visual,
                model=content_model,
            )
            result = dict(result)
            result["review_origin"] = "short_cinematic_cloud_visual_qa"
            result["vision_review_performed"] = True
            result["candidate_inspection_index"] = inspection_index
            return result

        # Alternate search text is retrieval-only. Keep the semantic Vision intent
        # stable on the beat's primary editorial visual, matching the long-form guard.
        stable_audit = _stable_intent_audit(audit_fn, primary_query)
        primary = _search_assets(primary_query, pexels_key=pexels_key, pixabay_key=pixabay_key)
        result = select_with_recovery(
            primary,
            portrait=True,
            target_seconds=shot_seconds,
            narration_context=_clean(event.get("text"), 280),
            intended_visual=primary_query,
            audit_fn=stable_audit,
            cache=cache,
            alternate_query_fn=lambda q=alternate_query: q,
            alternate_search_fn=lambda query: _search_assets(
                query, pexels_key=pexels_key, pixabay_key=pixabay_key
            ),
            max_candidates_per_attempt=MAX_VISION_REVIEWS_PER_ATTEMPT,
            max_total_inspections=MAX_TOTAL_INSPECTIONS_PER_BEAT,
        )
        for review in result.reviewed:
            visual_audits.append(
                {
                    "section": "s1",
                    "short_beat_id": f"b{beat_index:02d}",
                    "provider": review.provider,
                    "candidate_id": review.candidate.get("id"),
                    "from_cache": review.from_cache,
                    "intended_visual": primary_query,
                    "alternate_retrieval_used": result.used_alternate_query,
                    **review.audit,
                }
            )
        if result.status != "selected" or result.chosen is None:
            raise ShortCinematicError(
                f"Short cinematic Visual QA could not select a safe distinct asset for beat {beat_index}"
            )

        chosen = result.chosen.candidate
        provider = result.chosen.provider
        selected_query = result.alternate_query if result.used_alternate_query else primary_query
        downloadable = best_file(chosen, portrait=True)
        if not downloadable:
            raise ShortCinematicError(f"Short cinematic selected asset is not downloadable for beat {beat_index}")
        raw = _download_for_provider(
            provider,
            downloadable["link"],
            raw_root / f"shot-{beat_index:02d}-{provider}-raw.mp4",
        )
        prepared_clip = _prepare_m8_clip(
            raw,
            prepared_root / f"shot-{beat_index:02d}-{provider}-prepared.mp4",
            shot_seconds,
        )
        prepared.append(prepared_clip)

        user = chosen.get("user", {}) if isinstance(chosen.get("user"), dict) else {}
        credit = {
            "provider": provider,
            "query": selected_query,
            "asset_id": chosen.get("id"),
            "source_url": chosen.get("url"),
            "pexels_id": chosen.get("id") if provider == "pexels" else None,
            "pexels_url": chosen.get("url") if provider == "pexels" else None,
            "pixabay_id": chosen.get("id") if provider == "pixabay" else None,
            "pixabay_url": chosen.get("url") if provider == "pixabay" else None,
            "creator": user.get("name"),
            "creator_url": user.get("url"),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "rights_flags": _rights_flags(result.chosen.audit),
            "short_beat_id": f"b{beat_index:02d}",
            "short_cinematic_profile": PROFILE,
        }
        new_credits.append(credit)
        timeline_shots.append(
            {
                "shot_id": f"short-shot-{beat_index:02d}",
                "beat_id": f"b{beat_index:02d}",
                "role": _clean(event.get("role"), 30) or "beat",
                "start_seconds": round(float(event.get("start") or 0.0), 3),
                "end_seconds": round(float(event.get("end") or 0.0), 3),
                "provider": provider,
                "asset_id": chosen.get("id"),
                "query": selected_query,
                "intended_visual": primary_query,
                "source": "short_cinematic_audited_asset",
                "transition_in": "hard_cut",
            }
        )

    cinematic_picture = concat_video(prepared, work / "picture-short-cinematic-v1.mp4")
    expected = sum(_event_duration(item) for item in events)
    actual = duration(cinematic_picture)
    if abs(actual - expected) > 0.20:
        raise ShortCinematicError(
            f"Short cinematic timeline duration drift: expected={expected:.3f} actual={actual:.3f}"
        )

    progressive_picture = work / "picture-short-cinematic-text-v1.mp4"
    render_progressive_text(
        video=cinematic_picture,
        events=events,
        srt_path=root / "short-progressive-cinematic.srt",
        output=progressive_picture,
    )
    cinematic_final = work / "final-short-cinematic-v1.mp4"
    _remux_video_with_existing_audio(progressive_picture, final_path, cinematic_final)
    shutil.move(str(cinematic_final), str(final_path))

    timeline = {
        "schema_version": 1,
        "profile": PROFILE,
        "status": "applied",
        "template": template,
        "scope": scope,
        "duration_seconds": round(expected, 3),
        "shot_count": len(timeline_shots),
        "distinct_asset_count": len(
            {(str(item["provider"]), str(item["asset_id"])) for item in timeline_shots}
        ),
        "multi_asset_broll_generated": len(timeline_shots) >= 2,
        "beat_to_shot_binding": "one_semantic_beat_per_distinct_audited_asset",
        "transition_policy": "hard_cut_default_for_short_retention",
        "recent_visual_history_exclusion": True,
        "max_vision_reviews_per_additional_beat": MAX_VISION_REVIEWS_PER_BEAT,
        "zero_text_ai_calls": True,
        "shots": timeline_shots,
    }
    if timeline["distinct_asset_count"] != timeline["shot_count"]:
        raise ShortCinematicError("Short cinematic director produced duplicate final-cut assets")
    (root / "short-visual-timeline.json").write_text(
        json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (root / "visual-audit.json").write_text(
        json.dumps(visual_audits, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    rights_metadata = {
        "profile": PROFILE,
        "multi_asset_broll_generated": True,
        "shot_count": len(timeline_shots),
        "additional_stock_assets": len(new_credits),
        "all_additional_assets_visual_qa": True,
        "all_additional_assets_m8_normalized": True,
        "recent_visual_history_exclusion": True,
        "max_vision_reviews_per_additional_beat": MAX_VISION_REVIEWS_PER_BEAT,
        "transition_policy": timeline["transition_policy"],
    }
    _append_rights(root, credits, new_credits, rights_metadata)

    updated = dict(pre_gold)
    compensation = dict(updated.get("compensation") or {})
    compensation.update(
        {
            "profile": PROFILE,
            "multi_asset_broll_generated": True,
            "shot_count": len(timeline_shots),
            "distinct_asset_count": timeline["distinct_asset_count"],
            "beat_driven_visual_reframe_applied": False,
            "beat_driven_multi_shot_applied": True,
            "short_visual_timeline": "short-visual-timeline.json",
            "additional_visual_ai_calls_bounded": True,
            "max_vision_reviews_per_additional_beat": MAX_VISION_REVIEWS_PER_BEAT,
            "vision_reviews_per_retrieval_attempt": MAX_VISION_REVIEWS_PER_ATTEMPT,
            "recent_visual_history_exclusion": True,
            "m8_applied_to_additional_assets": True,
            "rights_refreshed": True,
        }
    )
    updated["compensation"] = compensation
    updated["short_cinematic"] = timeline
    return updated


def apply_short_sfx(output_dir: Path, pre_gold: dict[str, Any]) -> dict[str, Any]:
    """Add at most one subtle locally generated payoff accent to the voiced Short."""
    root = Path(output_dir)
    template = _clean(pre_gold.get("short_template"), 40)
    sfx_id = _SHORT_SFX_BY_TEMPLATE.get(template)
    final_path = root / "final.mp4"
    events = [item for item in list(pre_gold.get("timed_text_events") or []) if isinstance(item, dict)]
    report = {
        "schema_version": 1,
        "profile": "short_sfx_v1",
        "status": "not_applicable" if not sfx_id else "pending",
        "max_accents_per_short": 1,
        "rights_designation": "original_owned_procedural",
        "zero_ai_calls": True,
        "events": [],
    }
    if not sfx_id or len(events) < 2:
        (root / "short-sfx-plan.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return pre_gold
    try:
        payoff_time = float(events[-1].get("start") or 0.0)
    except (TypeError, ValueError):
        payoff_time = 0.0
    if payoff_time < 2.0:
        report["status"] = "not_applicable_timing"
        (root / "short-sfx-plan.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return pre_gold

    library = root / "short-sfx" / "library"
    materialize_sfx_library(library)
    extracted = root / "short-sfx" / "voiced-master.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(final_path), "-vn", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le", str(extracted),
        ],
        check=True,
    )
    event = SfxEvent(
        event_id="short-sfx-01",
        sfx_id=sfx_id,
        time_seconds=round(payoff_time, 3),
        gain_db=-23.0,
        reason="short_payoff_boundary",
        section_id="s1",
    )
    mixed = root / "short-sfx" / "voiced-master-sfx.wav"
    mix_sfx_into_narration(extracted, [event], library, mixed)
    remuxed = root / "short-sfx" / "final-short-sfx.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(final_path), "-i", str(mixed),
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
            "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-shortest", "-movflags", "+faststart", str(remuxed),
        ],
        check=True,
    )
    if not remuxed.is_file() or remuxed.stat().st_size <= 1024:
        raise ShortCinematicError("Short SFX pass did not produce a usable master")
    shutil.move(str(remuxed), str(final_path))

    report.update(
        {
            "status": "mixed",
            "events": [
                {
                    "event_id": event.event_id,
                    "sfx_id": event.sfx_id,
                    "time_seconds": event.time_seconds,
                    "gain_db": event.gain_db,
                    "reason": event.reason,
                }
            ],
        }
    )
    (root / "short-sfx-plan.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    rights = _read_json(root / "rights-manifest.json", dict)
    rights["short_sfx_v1"] = {
        "generated": True,
        "rights_designation": "original_owned_procedural",
        "third_party_sample_bytes": False,
        "event_count": 1,
    }
    (root / "rights-manifest.json").write_text(json.dumps(rights, ensure_ascii=False, indent=2), encoding="utf-8")

    updated = dict(pre_gold)
    compensation = dict(updated.get("compensation") or {})
    compensation.update(
        {
            "short_sfx_applied": True,
            "short_sfx_event_count": 1,
            "short_sfx_zero_ai_calls": True,
        }
    )
    updated["compensation"] = compensation
    updated["short_sfx"] = report
    return updated
