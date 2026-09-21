from __future__ import annotations

"""Thin Clean V2 adapter over the already-certified Security V1 + Cinematic V2 code.

This module deliberately does not reimplement Security V1 or M7-M11 policy. It only
translates Clean V2's simpler artifacts into the legacy kernels' existing interfaces.
Where Clean V2 lacks Director/Visual-QA evidence, M7 is forced through its historical
legacy-section fallback and M11 receives no invented Director scene plan.
"""

import json
import os
import shutil
from pathlib import Path
from typing import Any, Mapping

LAYER_ID = "security_v1_cinematic_v2_m7_m11"
REPORT_NAME = "security-cinematic-v2.json"


class CleanV2LayerBlock(RuntimeError):
    """A failure caused by the newly restored legacy quality/cinematic layer."""


def _block(stage: str, exc: Exception) -> CleanV2LayerBlock:
    return CleanV2LayerBlock(
        f"CLEAN_V2_NEW_LAYER_BLOCK stage={stage} "
        f"error={type(exc).__name__}:{str(exc)[:360]}"
    )


def security_query_normalizer(value: str) -> str:
    """Reuse Security V1's exact stock-query boundary; do not clone its schema."""
    try:
        from scripts.security_v1_live_binding import _normalized_stock_query

        return _normalized_stock_query(value)
    except Exception as exc:
        raise _block("security_v1.query", exc) from exc


def security_media_preflight(path: Path) -> dict[str, Any] | None:
    """Reuse Security V1's exact local multimodal firewall before asset admission."""
    try:
        from scripts.security_v1_live_binding import _stock_media_preflight

        return _stock_media_preflight(Path(path))
    except Exception as exc:
        raise _block("security_v1.media_preflight", exc) from exc


def m8_normalize_media(path: Path) -> Path:
    """Run the existing Engine M8 BT.709/SDR kernel in-place on an admitted clip."""
    path = Path(path)
    temporary = path.with_name(f".{path.stem}.m8-normalized{path.suffix}")
    temporary.unlink(missing_ok=True)
    try:
        from isco_video_agent.cinematic_m8_color_kernel import (
            normalize_to_bt709_sdr,
            report_dict,
        )

        report = normalize_to_bt709_sdr(path, temporary)
        os.replace(temporary, path)
        payload = {
            **report_dict(report),
            "status": "applied",
            "production_stage": "clean_v2_after_security_before_render",
            "source": path.name,
            "final_clip": path.name,
            "creative_grade_authority": "not_present_in_clean_v2",
        }
        path.with_suffix(".m8.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise _block("m8.color_normalization", exc) from exc


def _section_lookup(items: Mapping[str, Any] | dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    rows = items.get(key) if isinstance(items, dict) else None
    if not isinstance(rows, list):
        return {}
    return {
        str(row.get("id") or ""): row
        for row in rows
        if isinstance(row, dict) and str(row.get("id") or "")
    }


def _compatibility_plan(
    plan: dict[str, Any],
    script: dict[str, Any],
    rights: list[dict[str, Any]],
    *,
    fmt: str,
):
    """Build only the data shape M7's *existing legacy fallback* requires.

    No Director beats/scenes/candidate scores are fabricated. Each Clean V2 acquired
    final-cut asset remains authoritative and is bound to its persisted acquisition
    evidence in rights-manifest.json.
    """
    from isco_video_agent.models import ProductionPlan, ScriptSection

    plan_sections = _section_lookup(plan, "sections")
    script_sections = _section_lookup(script, "sections")
    ordered_plan_ids = list(plan_sections)
    sections: list[ScriptSection] = []
    used_ids: set[str] = set()

    for index, right in enumerate(rights):
        raw_id = str(right.get("section_id") or "").strip()
        if not raw_id and index < len(ordered_plan_ids):
            raw_id = ordered_plan_ids[index]
        section_id = raw_id or f"clean_v2_visual_{index + 1:02d}"
        if section_id in used_ids:
            section_id = f"{section_id}__visual_{index + 1:02d}"
        used_ids.add(section_id)

        base_id = raw_id if raw_id in plan_sections else (
            ordered_plan_ids[index] if index < len(ordered_plan_ids) else ""
        )
        p = plan_sections.get(base_id, {})
        s = script_sections.get(base_id, {})
        sections.append(
            ScriptSection(
                id=section_id,
                narration=str(s.get("narration") or ""),
                visual_query=str(p.get("visual_query_en") or right.get("query") or ""),
                on_screen_text=str(p.get("on_screen_text") or ""),
                emotion="reflective",
                expected_seconds=0.0,
                key_point=str(p.get("purpose") or p.get("heading") or "legacy_section"),
            )
        )
        right["m7_section_id"] = section_id

    if not sections:
        raise CleanV2LayerBlock(
            "CLEAN_V2_NEW_LAYER_BLOCK stage=m7.compatibility error=no_visual_assets"
        )

    return ProductionPlan(
        topic=str(plan.get("title") or "Clean V2"),
        pillar="clean_v2",
        format="moment" if fmt in {"moment", "story", "short"} else "film",
        hook=str(plan.get("promise") or ""),
        title_options=[str(plan.get("title") or "Clean V2")],
        thumbnail_concepts=[],
        sections=sections,
        cta="",
        closing_payoff="",
        narrative_format="direct_cinematic",
        editorial_intent={},
    )


def _slot_durations(total_seconds: float, count: int) -> list[float]:
    if count <= 0 or total_seconds <= 0:
        raise ValueError("positive duration and visual count required")
    base = total_seconds / count
    values = [base for _ in range(count)]
    values[-1] = total_seconds - sum(values[:-1])
    return values


def _m10_plan(
    compatibility_plan: Any,
    *,
    fmt: str,
) -> dict[str, Any]:
    return {
        "format": "moment" if fmt in {"moment", "story", "short"} else "film",
        "sections": [
            {
                "id": section.id,
                "narration": section.narration,
                "on_screen_text": section.on_screen_text,
            }
            for section in compatibility_plan.sections
        ],
    }


def _apply_m10_cards(
    final_path: Path,
    policy: dict[str, Any],
    *,
    output_dir: Path,
) -> dict[str, Any]:
    cards = list(policy.get("cards") or [])
    if not cards:
        return policy

    from isco_video_agent.cinematic_m10_cards import CardRequest, CardType, render_card
    from scripts.m10_live_binding import _layout

    temporary = output_dir / ".clean-v2-m10"
    temporary.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []
    current = Path(final_path)
    try:
        for index, item in enumerate(cards, 1):
            dest = temporary / f"card-{index:02d}.mp4"
            request = CardRequest(
                card_type=CardType.QUOTE if item["kind"] == "quote" else CardType.STAT,
                primary_text=str(item["primary_text"]),
                secondary_text=str(item.get("secondary_text") or ""),
                start_seconds=float(item["start_seconds"]),
                end_seconds=float(item["end_seconds"]),
            )
            current = Path(render_card(current, request, _layout(), dest))
            generated.append(current)
        os.replace(current, final_path)
        return policy
    except Exception as exc:
        fallback = dict(policy)
        fallback["status"] = "render_error_fallback_to_uncarded_video"
        fallback["render_error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
        return fallback
    finally:
        for item in generated:
            if item != current or item.exists():
                item.unlink(missing_ok=True)
            item.with_suffix(".m10.ass").unlink(missing_ok=True)
        for ass in temporary.glob("*.m10.ass"):
            ass.unlink(missing_ok=True)
        try:
            temporary.rmdir()
        except OSError:
            pass


def apply_post_render_layer(
    *,
    output_dir: Path,
    final_path: Path,
    narration_path: Path,
    plan: dict[str, Any],
    script: dict[str, Any],
    rights: list[dict[str, Any]],
    fmt: str,
) -> dict[str, Any]:
    """Execute the restored M7-M11 layer after the unchanged Clean V2 renderer.

    Security V1 and M8 are already executed at media acquisition through the hooks
    above. Here we invoke M7's certified legacy fallback, HEI metadata binding, M9's
    existing semantic-transition policy, M11's exact runtime with no fabricated
    Director scene plan, and M10's existing evidence-only card policy/renderer.
    """
    try:
        from isco_video_agent.cinematic_m7_visual_timeline import compile_visual_timeline
        from isco_video_agent.human_editorial_intent import apply_human_editorial_intent
        from isco_video_agent.cinematic_m11_runtime import apply_m11_overrides
        from clean_v2.media import probe_duration
        from scripts.m9_live_binding import plan_semantic_transitions
        from scripts.m10_live_binding import plan_evidence_cards

        output_dir = Path(output_dir)
        final_path = Path(final_path)
        total_seconds = probe_duration(Path(narration_path))
        compat_plan = _compatibility_plan(plan, script, rights, fmt=fmt)
        compat_plan_path = output_dir / "m7-compat-plan.json"
        compat_plan_path.write_text(
            json.dumps(compat_plan.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        durations = _slot_durations(total_seconds, len(compat_plan.sections))

        legacy_entries: list[dict[str, Any]] = []
        for index, (right, seconds) in enumerate(zip(rights, durations)):
            provider = str(right.get("provider") or "generated_local")
            asset_id = right.get("asset_id")
            if asset_id is None:
                raise ValueError(f"missing asset_id for rights row {index}")
            evidence = {
                "file": "rights-manifest.json",
                "json_pointer": f"/assets/{index}",
                "evidence_kind": "clean_v2_acquisition_security_provenance",
            }
            legacy_entries.append(
                {
                    "section_id": str(right["m7_section_id"]),
                    "duration_seconds": float(seconds),
                    "provider": provider,
                    "asset_id": asset_id,
                    "candidate_ref": f"{provider}:{asset_id}",
                    "source_url": right.get("source_url"),
                    "cut_reason": (
                        "episode_start" if index == 0 else "legacy_final_cut_boundary"
                    ),
                    "fallback_reason": "clean_v2_no_director_visual_qa",
                    "final_cut_audit_reference": evidence,
                    "rights_reference": evidence,
                }
            )

        timeline = compile_visual_timeline(
            compat_plan,
            section_durations=durations,
            section_audio_paths=[None] * len(durations),
            beat_plan=None,
            scene_plan=None,
            candidate_manifest=None,
            legacy_final_cut_visuals=legacy_entries,
            plan_path=compat_plan_path,
        )
        timeline = apply_human_editorial_intent(
            timeline,
            scene_plan=None,
            recent_signatures=[],
        )
        (output_dir / "visual-timeline.json").write_text(
            json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        m9_report = plan_semantic_transitions(timeline)
        (output_dir / "m9-transitions.json").write_text(
            json.dumps(m9_report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Clean V2 intentionally has no Director scene_plan. Invoke the exact M11
        # runtime with an empty scene set rather than inventing archive intent; its
        # certified behavior is therefore a truthful not_applicable fallback.
        _, _, m11_report = apply_m11_overrides(
            timeline,
            {"scenes": []},
            [],
            [],
            out=output_dir,
            fps=30,
            first_section_id=str(compat_plan.sections[0].id),
            review_fn=None,
        )

        m10_report = plan_evidence_cards(
            _m10_plan(compat_plan, fmt=fmt),
            timeline,
        )
        m10_report = _apply_m10_cards(
            final_path,
            m10_report,
            output_dir=output_dir,
        )
        (output_dir / "m10-cards.json").write_text(
            json.dumps(m10_report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        m8_reports = sorted(
            str(path.relative_to(output_dir))
            for path in (output_dir / "visuals").glob("*.m8.json")
        )
        report = {
            "schema_version": 1,
            "layer": LAYER_ID,
            "status": "pass",
            "reuse_not_rewrite": True,
            "ai_calls_added": 0,
            "security_v1": {
                "status": "enforced_at_stock_query_and_media_admission",
                "owner": "scripts.security_v1_live_binding",
            },
            "m7": {
                "status": "pass",
                "timeline_mode": timeline.get("timeline_mode"),
                "director_evidence_fabricated": False,
                "owner": "isco_video_agent.cinematic_m7_visual_timeline",
            },
            "m8": {
                "status": "pass",
                "normalized_assets": len(m8_reports),
                "reports": m8_reports,
                "owner": "isco_video_agent.cinematic_m8_color_kernel",
            },
            "m9": {
                "status": m9_report.get("status"),
                "owner": "scripts.m9_live_binding",
            },
            "m10": {
                "status": m10_report.get("status"),
                "owner": "scripts.m10_live_binding + engine cinematic_m10_cards",
            },
            "m11": {
                "status": m11_report.get("status"),
                "director_scene_plan_fabricated": False,
                "owner": "isco_video_agent.cinematic_m11_runtime",
            },
        }
        (output_dir / REPORT_NAME).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return report
    except CleanV2LayerBlock:
        raise
    except Exception as exc:
        raise _block("m7_m11.post_render", exc) from exc
