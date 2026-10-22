"""Simple Mode V1: a run-mode flag on the existing pipeline (not a second engine).

Motivation (Runs 109-123): no run reached a finished video because subjective
LLM verdicts (tone audit, per-clip semantic review, opening review) decided whether
a video could exist at all. Simple Mode keeps every measurable safety check and the
proven script/voice/resume/edit path, and changes three things only:

1. Tone/naturalness verdicts are advisory unless they cite an objective problem
   (unverified religious quote, cultural dignity). Factuality stays blocking.
2. Stock visuals are ranked once, then the existing canonical pixel reviewer is
   called at most once per chosen asset. Missing proof or safety failure replaces
   that source with a disclosed local symbolic scene, without search/review loops.
   The existing subjective 0.85 score never gates this mode's production.
3. An honest report separates technical completion from editorial readiness.

The flag is set by the immutable Telegram request, never mid-run. The default path is
untouched when the flag is absent.
"""
from __future__ import annotations

import os
import json
import hashlib
from pathlib import Path
from typing import Any, Mapping

from clean_v2.contracts import atomic_write_json

SIMPLE_MODE_ENV = "CLEAN_V2_SIMPLE_MODE"
FALLBACK_PROVIDER = "generated_local"
# Images the owner designed himself: never replaced or reviewed, never colour-matched.
OWNER_ART_PROVIDER = "owner_art"
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


def _use_approved_image(clip: Path, row: dict[str, Any], beat: Mapping[str, Any], *,
                        index: int, fmt: str, seconds: float, reason: str) -> bool:
    """A rejected optional stock clip is replaced by the owner's approved image, never by a bare background.

    Only rows that asked for stock over an approved package image qualify; everything else
    keeps the existing local replacement. Returns True when the clip file was rewritten.
    """
    approved = str(row.get("approved_fallback_asset") or "")
    if not (row.get("stock_requested") and approved):
        return False
    try:
        from .owner_art import PROVIDER, art_dir, make_motion_clip

        directory = art_dir()
        asset = directory / approved if directory else None
        if asset is None or not asset.is_file():
            return False
        rejected = {k: row.get(k) for k in ("provider", "asset_id", "source_url", "creator", "source_actual")}
        # The pipeline hands visual QA fmt="story" for a Short: portrait must follow the same
        # set the renderer uses, or the replacement is a 16:9 clip centre-cropped to ~3x.
        make_motion_clip(asset, clip, seconds=float(seconds) + 1, direction=index,
                         portrait=fmt in {"short", "story", "moment"},
                         motion_kind=str(beat.get("owner_motion") or "slow_pan"), respect_hold=True)
    except Exception:
        return False
    row.update(provider=PROVIDER, source_actual=PROVIDER, asset_id=approved, source_url="",
               creator="owner supplied", creator_url="", attribution_required=False, owner_approved=True,
               visual_verification="owner_designed_art", replacement_reason=reason, rejected_source=rejected)
    row.pop("visual_evidence", None)
    return True


STOCK_CONTINUE_MIN_SECONDS = 1.5   # a stock clip shorter than this is not worth a scene
STOCK_CONTINUE_FADE_SECONDS = 0.8  # dissolve from the short stock clip into the approved image


def _continue_short_clip_with_image(clip: Path, row: dict[str, Any], beat: Mapping[str, Any], *,
                                    index: int, fmt: str, seconds: float) -> bool:
    """A reviewed stock clip shorter than its slot plays first; the scene's approved image finishes the slot.

    Same idea as a pinned link (owner_art._try_pinned): the real moving clip is kept for as long as it lasts,
    then dissolves into the owner's approved image. Never loops, reverses or stretches the clip. Returns True
    when ``clip`` was rewritten to the full slot length; False leaves it untouched (caller falls back to the
    plain approved-image replacement).
    """
    approved = str(row.get("approved_fallback_asset") or "")
    if not (row.get("stock_requested") and approved):
        return False
    try:
        from .media import _run, probe_duration
        from .owner_art import CLIP_HEIGHT, CLIP_WIDTH, art_dir, make_motion_clip

        length = probe_duration(clip)
        if length < STOCK_CONTINUE_MIN_SECONDS or length + 0.08 >= seconds:
            return False
        directory = art_dir()
        asset = directory / approved if directory else None
        if asset is None or not asset.is_file():
            return False
        portrait = fmt in {"short", "story", "moment"}
        fade = STOCK_CONTINUE_FADE_SECONDS
        tail = clip.with_name(f"{clip.stem}.tail.mp4")
        joined = clip.with_name(f"{clip.stem}.joined.mp4")
        make_motion_clip(asset, tail, seconds=seconds - length + fade + 0.5, direction=index, portrait=portrait,
                         motion_kind=str(beat.get("owner_motion") or "slow_pan"), respect_hold=True)
        width, height = (720, 1280) if portrait else (CLIP_WIDTH, CLIP_HEIGHT)
        graph = (f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
                 "fps=30,setsar=1,format=yuv420p[a];[1:v]fps=30,setsar=1,format=yuv420p[b];"
                 f"[a][b]xfade=transition=fade:duration={fade}:offset={length - fade:.3f}[v]")
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip), "-i", str(tail),
              "-filter_complex", graph, "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "ultrafast",
              "-crf", "22", "-pix_fmt", "yuv420p", str(joined)], timeout=300)
        tail.unlink(missing_ok=True)
        if not joined.is_file() or joined.stat().st_size <= 0 or probe_duration(joined) + 0.2 < seconds:
            joined.unlink(missing_ok=True)
            return False
        joined.replace(clip)
    except Exception:
        for name in (f"{clip.stem}.tail.mp4", f"{clip.stem}.joined.mp4"):
            clip.with_name(name).unlink(missing_ok=True)
        return False
    row.update(stock_continues_with_image=True, stock_source_seconds=round(length, 2))
    return True


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
    """One existing pixel review per selected asset; no recovery/search/retry loop.

    Objective proof/policy failures replace that asset locally and do not stop
    production. Scores for attractive lighting or mood are advisory. Unavailable
    evidence never becomes approval. A provider outage disables later calls.
    """
    from .media import create_fallback_visual, local_visual_recipe, _sha256, probe_duration, _read_secret
    from .visual_qa import _apply_observed_visual_proof, _infrastructure_error, _review_display_durations
    from .visual_story import visual_review_context

    rows = [row for row in rights if isinstance(row, Mapping)]
    scripts = {str(s.get("id")): str(s.get("narration") or "") for s in script.get("sections", []) if isinstance(s, Mapping)}
    story = plan.get("visual_story") or {}
    if not story:
        try:
            story = json.loads((Path(output_dir) / "visual-story.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            story = {}
    beats = {str(b.get("id")): b for b in story.get("beats", []) if isinstance(b, Mapping)}
    display = _review_display_durations(Path(output_dir), rights, fmt) if rows and all(r.get("local_file") for r in rows) else {}
    reviews: list[dict[str, Any]] = []
    review_calls = 0
    # Reuse the existing route that proves a free account BEFORE inference and
    # shares the workflow's five-call ceiling. Never bypass production's Gemini
    # skip policy or infer that another API key implies a zero-cost account.
    ledger = None
    review_available = (os.environ.get("CLOUDFLARE_VISION_FREE_ONLY", "").strip().lower() == "true"
                        and bool(_read_secret("CLOUDFLARE_API_TOKEN"))
                        and bool(_read_secret("CLOUDFLARE_ACCOUNT_ID")))
    if review_available:
        from isco_video_agent.ai_budget import BudgetLedger, Capability, Priority, TaskSpec
        from scripts import gold_cloudflare_vision_fallback as free_vision
        from scripts.vision_stage_contract_v2 import VisionStageError
        ledger = BudgetLedger(fmt, enforce=True)
        review_available = free_vision.shared_vision_configured()
    previous_observation = ""
    for index, row in enumerate(rows):
        local_file = str(row.get("local_file") or "")
        clip = Path(output_dir) / "visuals" / local_file
        if not local_file or not clip.is_file():
            reviews.append({"local_file": local_file, "decision": "evidence_missing"})
            continue  # Structural render checks still reject an actually missing source.
        beat = dict(beats.get(str(row.get("beat_id"))) or row)
        narration = scripts.get(str(row.get("section_id")), "")
        fallback_intent = str(row.get("shot_intent") or row.get("query") or "")
        context = visual_review_context(story, str(row.get("beat_id")), fallback_intent, previous_observation=previous_observation) if beats else fallback_intent
        context += (
            " Mandatory source policy: no runway/fashion posing or aimless walking; "
            "no unrelated animals, roads or landscape filler. No readable or pseudo "
            "lettering inside source pixels. Preserve an actual observable action/state "
            "from this narration; do not infer it from search metadata."
        )
        context_hash = hashlib.sha256(json.dumps({
            "narration": narration, "intent": context,
            "display_seconds": display.get(local_file),
            "display_aspect": "9:16" if fmt in {"short", "story", "moment"} else "16:9",
        }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        source_hash = _sha256(clip)
        saved = row.get("visual_evidence") or {}
        audit: dict[str, Any] = {}
        provenance: dict[str, Any] = {}
        reason = (
            "local_source" if row.get("provider") in {FALLBACK_PROVIDER, OWNER_ART_PROVIDER}
            else "visual_review_unavailable"
        )
        if (isinstance(saved, Mapping) and saved.get("source_sha256") == source_hash
                and saved.get("context_sha256") == context_hash and saved.get("frame_sha256") and saved.get("prompt_hash")):
            audit = dict(saved.get("audit") or {})
            provenance = dict(saved)
            reason = "reused_exact_pixel_evidence"
        elif row.get("provider") not in {FALLBACK_PROVIDER, OWNER_ART_PROVIDER} and review_available:
            try:
                from scripts.canonical_visual_evidence_v1 import build_canonical_visual_evidence
                evidence = build_canonical_visual_evidence(
                    clip, Path(output_dir) / "visual-evidence" / f"simple-{index + 1:02d}",
                    narration_context=narration, intended_visual=context,
                    display_aspect_ratio=(9, 16) if fmt in {"short", "story", "moment"} else (16, 9),
                    duration_limit_seconds=display.get(local_file),
                )
                spec = TaskSpec(task_id=f"SIMPLE_VISUAL_AUDIT_{index + 1:03d}", kind="VISUAL_AUDIT",
                                priority=Priority.P0, capability=Capability.VISION,
                                max_provider_attempts=1, schema_repair_allowed=False,
                                local_fallback=False, semantic_block_is_final=True)
                ledger.register_task(spec)
                # Call the existing single free route directly, without installing
                # the standard recovery mesh or retrying a rejected scene.
                audit = free_vision.run_shared_cloudflare_attempt(
                    ledger, spec, preview=clip, canonical_visual_evidence=evidence,
                    narration_context=narration, intended_visual=context,
                )
                provenance = {"source_sha256": source_hash, "context_sha256": context_hash,
                              "prompt_hash": evidence.prompt_hash, "frame_sha256": list(evidence.frame_sha256),
                              "review_provider": free_vision.CLOUDFLARE_VISION_PROVIDER, "audit": dict(audit)}
                reason = "single_canonical_pixel_review"
            except Exception as exc:
                if not (isinstance(exc, (free_vision.CloudflareGoldVisionUnavailable, VisionStageError))
                        or _infrastructure_error(exc) or is_known_infra_failure(exc)):
                    raise
                review_available = False
                reason = f"review_unavailable:{type(exc).__name__}"
        if row.get("provider") == OWNER_ART_PROVIDER:
            # The owner designed this image; nothing may review or replace it.
            row["visual_verification"] = "owner_designed_art"
            reviews.append({"local_file": local_file, "decision": "owner_designed_art", "reason": "owner_art"})
            continue
        audit = _apply_observed_visual_proof(audit)
        risk_fields = ("identifiable_person", "sensitive_trait_implication_risk", "prominent_logo_or_brand",
                       "cultural_conflict", "cultural_islamic_suitability_risk", "advertiser_conflict", "obvious_synthetic_or_visual_artifact")
        accepted = bool(
            audit.get("observed_visual") and audit.get("observed_proof_status") == "matched"
            and audit.get("observed_face_status") in {"none", "unrecognizable"}
            and all(audit.get(k) is False for k in risk_fields)
        )
        if accepted:
            from .media import _stock_metadata_context_conflict
            if _stock_metadata_context_conflict(str(row.get("query") or ""), str(audit.get("observed_visual") or "")):
                accepted = False
                reason = "observed_source_policy_conflict"
        if accepted and row.get("source_actual") == "stock_motion" and display.get(local_file) and probe_duration(clip) + .08 < display[local_file]:
            # Keep the verified moving part, then let the scene's approved image finish the slot.
            # Without an approved image (or if the join fails) the clip is rejected as before.
            if _continue_short_clip_with_image(clip, row, beat, index=index, fmt=fmt, seconds=float(display[local_file])):
                provenance = {**provenance, "source_sha256": _sha256(clip),
                              "continuation": "stock_then_owner_approved_image"}
            else:
                accepted = False
                reason = "source_too_short_without_replay"
        # An owner-picked clip is the owner's editorial decision. The automatic
        # review may only override it for an objective safety/policy risk (a
        # recognisable face or a true risk flag), never for "not proven matched".
        # A clip the editor pinned by link is judged like an owner pick: the review still runs and an
        # objective risk (face, logo, brand, cultural, artefact) replaces it with the approved image,
        # but "not proven matched" or an unavailable review does not, because the editor chose it.
        pinned = bool(row.get("stock_pinned"))
        owner_pick = (row.get("human_choice") == "candidate" or pinned) and row.get("provider") != FALLBACK_PROVIDER
        objective_risk = (
            any(audit.get(k) is True for k in risk_fields)
            or audit.get("observed_face_status") not in {None, "", "none", "unrecognizable", "unknown", "unavailable"}
        )
        if owner_pick and not accepted and not objective_risk:
            row["visual_verification"] = "editor_pinned_unverified" if pinned else "owner_chosen_unverified"
            row["owner_choice_kept"] = reason
            if provenance:
                row["visual_evidence"] = provenance
            previous_observation = str(audit.get("observed_visual") or previous_observation)
            reason = f"{'pinned' if pinned else 'owner'}_choice_kept:{reason}"
            reviews.append({"local_file": local_file, "decision": "owner_chosen_unverified", "reason": reason,
                            "observed_visual": audit.get("observed_visual"), "proof": audit.get("observed_proof_status"),
                            "face": audit.get("observed_face_status"), "aesthetic_scores_advisory": True})
            continue
        if accepted:
            row["visual_evidence"] = provenance
            row["visual_verification"] = "observed_matched"
            previous_observation = str(audit["observed_visual"])
        elif _use_approved_image(clip, row, beat, index=index, fmt=fmt,
                                 seconds=display.get(local_file, 12.0), reason=reason):
            reviews.append({"local_file": local_file, "decision": "owner_designed_art",
                            "reason": f"stock_rejected_approved_image:{reason}",
                            "observed_visual": audit.get("observed_visual"), "proof": audit.get("observed_proof_status"),
                            "face": audit.get("observed_face_status"), "aesthetic_scores_advisory": True})
            continue
        else:
            recipe = local_visual_recipe(beat)
            create_fallback_visual(clip, portrait=fmt in {"short", "story", "moment"}, beat=beat,
                                   seconds=max(1, display.get(local_file, 12.0)))
            if visual_source is not None and getattr(visual_source, "media_preflight", None):
                blocked = visual_source.media_preflight(clip)
                if blocked is not None:
                    raise RuntimeError("simple local source failed security preflight")
            if visual_source is not None and getattr(visual_source, "media_transform", None):
                visual_source.media_transform(clip)
            rejected = {k: row.get(k) for k in ("provider", "asset_id", "source_url", "creator", "source_actual")}
            row.update(provider=FALLBACK_PROVIDER, source_actual=FALLBACK_PROVIDER,
                       asset_id=f"local-{row.get('beat_id') or index}", source_url="local://clean-v2-symbolic-scene",
                       creator="Isco Clean V2 local construction", creator_url="", attribution_required=False,
                       visual_verification="local_symbolic" if recipe != "channel_only" else "unverified_channel_only",
                       local_visual_recipe=recipe, replacement_reason=reason, rejected_source=rejected)
            row.pop("visual_evidence", None)
            previous_observation = f"local symbolic scene: {recipe}"
        reviews.append({"local_file": local_file, "decision": row.get("visual_verification"), "reason": reason,
                        "observed_visual": audit.get("observed_visual"), "proof": audit.get("observed_proof_status"),
                        "face": audit.get("observed_face_status"), "aesthetic_scores_advisory": True})
    manifest_path = Path(output_dir) / "rights-manifest.json"
    if manifest_path.is_file():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["assets"] = rights
        atomic_write_json(manifest_path, payload)
    primary = [row for row in rows if not row.get("pacing_auxiliary")]
    extra = [row for row in rows if row.get("pacing_auxiliary")]

    def _is_bg(row: Mapping[str, Any]) -> bool:
        return row.get("provider") == FALLBACK_PROVIDER

    backgrounds = [row for row in primary if _is_bg(row)]
    extra_backgrounds = [row for row in extra if _is_bg(row)]
    # Account for real inference attempts, not no-wire subscription/quota probes.
    review_calls = ledger.to_summary()["provider_attempts"]["total"] if ledger is not None else 0
    report = {
        "status": "pass",
        "mode": "simple_single_pass_local_replacement",
        "semantic_review": "single_pass" if review_calls else (
            "reused_exact_pixel_evidence" if any(r.get("decision") == "observed_matched" for r in reviews) else "not_performed"),
        "clips": len(primary),
        "stock_clips": len(primary) - len(backgrounds) - sum(r.get("provider") == OWNER_ART_PROVIDER for r in primary),
        "fallback_backgrounds": len(backgrounds),
        "extra_pacing_clips": len(extra),
        "extra_pacing_stock": len(extra) - len(extra_backgrounds),
        "extra_pacing_backgrounds": len(extra_backgrounds),
        "final_media_mutated": any(r.get("decision") in {"local_symbolic", "unverified_channel_only"} for r in reviews),
        "provider_calls_added": review_calls,
        "review_provider": "cloudflare_workers_ai",
        "zero_cost_policy": "existing_subscription_proof_and_shared_workflow_quota",
        "reviews": reviews,
        "observed_matched_clips": sum(r.get("decision") == "observed_matched" for r in reviews),
        "local_symbolic_clips": sum(r.get("decision") == "local_symbolic" for r in reviews),
        "uncertain_clips": sum(r.get("decision") in {"unverified_channel_only", "evidence_missing"} for r in reviews),
        "owner_chosen_unverified_clips": sum(r.get("decision") == "owner_chosen_unverified" for r in reviews),
        "recovery_searches": 0,
        "review_attempts_per_clip_max": 1,
    }
    owner_art_clips = sum(r.get("provider") == OWNER_ART_PROVIDER for r in primary)
    if owner_art_clips:  # key only exists for owner Podcasts; every other report is unchanged
        report["owner_art_clips"] = owner_art_clips
    atomic_write_json(Path(output_dir) / "final-cut-visual-qa.json", report)
    return report


def simple_opening_director(**_kwargs: Any) -> dict[str, Any]:
    """The opening reviewer is an LLM gate; simple mode renders the selected clips as-is."""
    return {"status": "skipped_simple_mode", "slots": []}


def stock_outcomes(output_dir: Path) -> dict[str, Any]:
    """What happened to every scene that asked for stock: used, or the recorded reason it was not.

    Read from the final rights manifest, so it reflects both the metadata check at import time and
    the later picture review. Empty when no scene asked for stock, so other reports stay unchanged.
    """
    import json

    try:
        payload = json.loads((Path(output_dir) / "rights-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = [r for r in (payload.get("assets") or []) if isinstance(r, Mapping) and r.get("stock_requested")] \
        if isinstance(payload, Mapping) else []
    if not rows:
        return {}
    used, fallbacks = 0, []
    for row in rows:
        if row.get("source_actual") in {"stock_motion", "stock_still"} and not row.get("owner_approved"):
            used += 1
        else:
            fallbacks.append({"scene": str(row.get("beat_id") or ""),
                              "reason": str(row.get("replacement_reason") or row.get("stock_fallback_reason") or "unknown")[:160]})
    pinned = [r for r in rows if r.get("stock_pinned_requested")]
    pinned_used = sum(1 for r in pinned if r.get("stock_pinned") and r.get("source_actual") in {"stock_motion", "stock_still"}
                      and not r.get("owner_approved"))
    continued = sum(1 for r in rows if r.get("stock_continues_with_image"))
    return {"requested": len(rows), "used": used, "fallbacks": fallbacks,
            **({"continued_with_approved_image": continued} if continued else {}),
            **({"pinned_requested": len(pinned), "pinned_used": pinned_used,
                "pinned_continued_with_image": sum(1 for r in pinned if r.get("pinned_continues_with_image"))} if pinned else {})}


def film_motion_preflight(rights: list[dict[str, Any]]) -> dict[str, Any]:
    """Guard costly Film render when most requested moving scenes became stills.

    Do not approve unknown stock pixels merely to fill a quota. This is an
    editorial admission check, not a replacement for safety/semantic review.
    It runs *after* final-cut QA and *before* any expensive full-film render.
    """
    import math
    from collections import Counter

    primary = [row for row in rights if isinstance(row, Mapping)
               and not row.get("pacing_auxiliary")]
    requested = [row for row in primary if row.get("stock_requested")
                 and str(row.get("source_preference") or "") != "stock_still"]
    used = [row for row in requested
            if row.get("source_actual") == "stock_motion"
            and not row.get("owner_approved")]
    needed = min(5, max(2, math.ceil(len(requested) / 4))) if len(requested) >= 6 else 0
    reason_counts = Counter(
        str(row.get("replacement_reason") or row.get("stock_fallback_reason") or "unverified_or_not_selected")
        for row in requested if row not in used
    )
    status = "needs_verified_motion" if needed and len(used) < needed else "pass"
    return {
        "status": status,
        "format": "film",
        "requested_motion_scenes": len(requested),
        "verified_or_owner_selected_motion_scenes": len(used),
        "minimum_motion_scenes": needed,
        "reason_counts": dict(sorted(reason_counts.items())),
        "policy": "no_unverified_clip_admission_and_no_zero-motion_film_render",
    }


def _story_pacing_audit(output_dir: Path, fmt: str) -> dict[str, Any]:
    """Advisory-only: no repeat-image illusion or forced edits to approved narration.

    Existing ZIPs remain renderable. New Ala Mahl must be reviewed if a 20-second
    still has no story turn, and a long Podcast with one photograph is disclosed.
    """
    import json

    output_dir = Path(output_dir)
    try:
        doc = json.loads((output_dir / "production.json").read_text(encoding="utf-8"))
        rights = json.loads((output_dir / "rights-manifest.json").read_text(encoding="utf-8"))
        timeline = json.loads((output_dir / "timeline-first.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(doc, dict) or not isinstance(rights, dict) or not isinstance(timeline, dict):
        return {}
    is_ala = doc.get("template") == "ala_mahl"
    if fmt != "podcast" and not is_ala:
        return {}
    duration = float(timeline.get("voice_seconds_measured") or 0)
    scene_rows = [row for row in rights.get("assets", []) if isinstance(row, dict)
                  and not row.get("pacing_auxiliary")]
    scenes = [item for item in doc.get("scenes", []) if isinstance(item, dict)]
    unique_images = len({row.get("asset") for row in scenes if row.get("asset")})
    warnings = []
    if fmt == "podcast" and duration >= 120 and unique_images < 2:
        warnings.append("podcast_single_photo_for_long_dialogue")
    if is_ala and duration > 55:
        warnings.append("ala_mahl_longer_than_55_seconds")
    long_stills = []
    try:
        from .visual_qa import _review_display_durations
        slot_lengths = _review_display_durations(output_dir, scene_rows, fmt)
    except (OSError, ValueError, KeyError, TypeError):
        slot_lengths = {}
    if is_ala:
        for row in scene_rows:
            if row.get("source_actual") not in {OWNER_ART_PROVIDER, FALLBACK_PROVIDER, "stock_still"}:
                continue
            secs = float(slot_lengths.get(str(row.get("local_file") or "")) or 0)
            if secs > 20:
                long_stills.append({"scene": str(row.get("beat_id") or ""),
                                    "seconds": round(secs, 2)})
        if long_stills:
            warnings.append("ala_mahl_still_over_20_seconds")
        if unique_images < 3 and duration > 40:
            warnings.append("ala_mahl_not_enough_distinct_story_moments")
    return {"format": fmt, "template": doc.get("template"), "voice_seconds": round(duration, 2),
            "distinct_approved_images": unique_images, "long_stills": long_stills,
            "warnings": warnings, "status": "review_required" if warnings else "no_flag"}


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
    owner_art_clips = int(qa.get("owner_art_clips") or 0)
    stock = int(qa.get("stock_clips") or max(0, scenes - backgrounds - owner_art_clips))
    extra = int(qa.get("extra_pacing_clips") or 0)
    extra_bg = int(qa.get("extra_pacing_backgrounds") or 0)
    share = round(backgrounds / scenes, 3) if scenes else 0.0
    report_tone = tone_audit_state(output_dir)
    from .scene_review import report_block

    scene_review_report = report_block(output_dir)
    report = {
        "mode": "simple",
        "format": fmt,
        "scenes": scenes,
        "stock_clips_selected_by_ranking": stock,
        "fallback_backgrounds": backgrounds,
        "fallback_share": share,
        "extra_pacing_clips": extra,
        "extra_pacing_backgrounds": extra_bg,
        "semantic_visual_review": qa.get("semantic_review", "not_performed"),
        "tone_audit": tone_audit_state(output_dir),
        "technically_complete": True,
        "render_success": True,
        "editorial_ready": False,
        "editorial_status": "owner_visual_review_required",
        "observed_matched_clips": int(qa.get("observed_matched_clips") or 0),
        "local_symbolic_clips": int(qa.get("local_symbolic_clips") or 0),
        "uncertain_clips": int(qa.get("uncertain_clips") or 0),
        "visual_review_calls": int(qa.get("provider_calls_added") or 0),
        "publish_decision": "owner",
        "scene_review": scene_review_report,
        **({"owner_designed_art_clips": owner_art_clips} if owner_art_clips else {}),
        **({"stock_scenes": stock_scenes} if (stock_scenes := stock_outcomes(output_dir)) else {}),
        "statement": (
            f"{scenes} scenes: {stock} stock clips chosen by similarity ranking, "
            + (f"{owner_art_clips} owner-designed images, " if owner_art_clips else "")
            + f"{backgrounds} channel backgrounds"
            + (f" (plus {extra} extra pacing clips, {extra_bg} of them backgrounds)" if extra else "")
            + f". {int(qa.get('observed_matched_clips') or 0)} assets have observed pixel proof; "
            + f"{int(qa.get('local_symbolic_clips') or 0)} are disclosed local symbolic scenes. "
            + "The finished composition is NOT certified editorial_ready; the owner must review it before publishing."
            + (" Source visual match was not reviewed." if not qa.get("provider_calls_added") and not qa.get("observed_matched_clips") else "")
            + (" The tone audit was unavailable and was NOT passed." if report_tone == "unavailable" else "")
            + (
                f" Owner scene review (all visual slots): {scene_review_report.get('human_chosen_clips', 0)} chosen, "
                f"{scene_review_report.get('human_background', 0)} channel background, "
                f"{scene_review_report.get('kept_automatic', 0)} left automatic, "
                f"{scene_review_report.get('human_choice_failed', 0)} selected clips unavailable, "
                f"{scene_review_report.get('unmarked_visual_slots', 0)} unmarked."
                if scene_review_report.get("mode") == "human_reviewed"
                else (" Scenes were NOT reviewed by a human (owner chose automatic selection)."
                      if scene_review_report.get("mode") == "auto_all_by_owner_decision" else "")
            )
        ),
    }
    pacing = _story_pacing_audit(output_dir, fmt)
    if pacing:
        report["story_pacing"] = pacing
        if pacing["warnings"]:
            report["editorial_status"] = "visual_pacing_review_required"
            report["statement"] += " Visual pacing requires owner review: " + ", ".join(pacing["warnings"]) + "."
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
