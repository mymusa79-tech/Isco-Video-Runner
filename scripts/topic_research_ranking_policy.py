"""Multi-stage ranking policy for Telegram Topic Research V2.

Measured market evidence remains owned by the pinned Research Engine. This module
owns only the post-measurement selection policy: market admission/classification,
separate channel/creative/execution scoring, diversity, and honest Telegram labels.
It never promotes unmeasured/fallback candidates and never relaxes production or
quality gates.
"""

from __future__ import annotations

from typing import Any

from scripts import telegram_topic_memory_ui as memory_ui

RESEARCH_CONTRACT_VERSION = "topic-research-v2"
POLICY_VERSION = "topic-ranking-multistage-v1"
STRONG_CURRENT_INTEREST_MIN = 0.65
HYBRID_CURRENT_INTEREST_MIN = 0.50
EVERGREEN_STRENGTH_MIN = 0.70
MIN_OPPORTUNITY_SCORE = 0.70
SURFACE_MARKET_CLASSES = ("rising", "hybrid", "evergreen")


def _market_class(candidate: dict[str, Any]) -> str:
    if (
        str(candidate.get("market_timing_status") or "") != "measured"
        or str(candidate.get("source_mode") or "") != "live_research"
    ):
        return "unverified"
    trend = float(candidate.get("trend_score", 0.0) or 0.0)
    evergreen = float(candidate.get("evergreen_score", 0.0) or 0.0)
    if trend >= STRONG_CURRENT_INTEREST_MIN:
        return "rising"
    if trend >= HYBRID_CURRENT_INTEREST_MIN and evergreen >= EVERGREEN_STRENGTH_MIN:
        return "hybrid"
    if evergreen >= EVERGREEN_STRENGTH_MIN:
        return "evergreen"
    return "explore"


def _market_class_ar(candidate: dict[str, Any]) -> str:
    return {
        "rising": "🔥 فرصة الآن: زخم حديث قوي",
        "hybrid": "⚖️ هجين: زخم متوسط + قيمة مستمرة",
        "evergreen": "🌲 Evergreen قوي — ليس فرصة حالية",
        "explore": "🧭 غير مؤهل للعرض الرئيسي",
        "unverified": "⚪ توقيت السوق غير متحقق",
    }[_market_class(candidate)]


def _channel_score(candidate: dict[str, Any]) -> float:
    audience = float(candidate.get("audience_fit", 0.0) or 0.0)
    evergreen = float(candidate.get("evergreen_score", 0.0) or 0.0)
    return round(0.62 * audience + 0.38 * evergreen, 3)


def _creative_score(candidate: dict[str, Any], kind: str) -> float:
    hook = float(candidate.get("hook_potential", 0.0) or 0.0)
    retention = float(candidate.get("retention_potential", 0.0) or 0.0)
    emotional = float(candidate.get("emotional_pull", 0.0) or 0.0)
    packaging = float(candidate.get("title_thumbnail_potential", 0.0) or 0.0)
    competition = float(candidate.get("competition_opportunity", 0.0) or 0.0)
    if kind == "short":
        return round(
            0.34 * hook
            + 0.34 * retention
            + 0.16 * emotional
            + 0.08 * packaging
            + 0.08 * competition,
            3,
        )
    return round(
        0.22 * hook
        + 0.30 * retention
        + 0.18 * emotional
        + 0.24 * packaging
        + 0.06 * competition,
        3,
    )


def _execution_score(candidate: dict[str, Any]) -> float:
    evidence = float(candidate.get("evidence_quality", 0.0) or 0.0)
    feasibility = float(candidate.get("production_feasibility", 0.0) or 0.0)
    return round(0.60 * evidence + 0.40 * feasibility, 3)


def _ranking_components(candidate: dict[str, Any], kind: str) -> dict[str, float]:
    return {
        "market": round(float(candidate.get("trend_score", 0.0) or 0.0), 3),
        "channel": _channel_score(candidate),
        "creative": _creative_score(candidate, kind),
        "execution": _execution_score(candidate),
    }


def _control_score(candidate: dict[str, Any], kind: str) -> float:
    """Composite score used only after market role/admission is determined."""
    component = _ranking_components(candidate, kind)
    if kind == "short":
        return round(
            0.23 * component["market"]
            + 0.22 * component["channel"]
            + 0.40 * component["creative"]
            + 0.15 * component["execution"],
            3,
        )
    return round(
        0.23 * component["market"]
        + 0.30 * component["channel"]
        + 0.32 * component["creative"]
        + 0.15 * component["execution"],
        3,
    )


def _candidate_reasons(candidate: dict[str, Any], kind: str) -> list[str]:
    reasons = {
        "rising": ["زخم حالي قوي ببيانات YouTube حديثة"],
        "hybrid": ["زخم متوسط مدعوم بقيمة Evergreen قوية"],
        "evergreen": ["قيمة Evergreen قوية رغم أن الزخم الحالي دون 5/10"],
        "explore": ["الزخم وEvergreen لا يكفيان للعرض الرئيسي"],
        "unverified": ["توقيت السوق غير متحقق"],
    }[_market_class(candidate)]
    components = _ranking_components(candidate, kind)
    labels = {
        "channel": "ملاءمة قوية لجمهور القناة",
        "creative": "قابلية إبداعية قوية للـHook والاحتفاظ والتغليف",
        "execution": "ثقة تنفيذ جيدة من الدليل وقابلية الإنتاج",
    }
    best = max(labels, key=lambda name: components[name])
    reasons.append(labels[best])
    return reasons[:2]


def _build_candidate_payload(candidate: dict[str, Any], kind: str) -> dict[str, Any]:
    from scripts import telegram_control_panel as panel

    normalized = dict(candidate)
    # Ranking is an extension of the existing V2 research contract, not a new
    # evidence contract. Always restore the exact contract marker even for thin
    # adapters/tests so downstream Short/Long approval code sees one stable schema.
    normalized["research_contract_version"] = RESEARCH_CONTRACT_VERSION
    normalized["market_class"] = _market_class(normalized)
    normalized["ranking_policy_version"] = POLICY_VERSION
    normalized["ranking_kind"] = kind
    normalized["market_thresholds"] = {
        "strong_current_interest_min": STRONG_CURRENT_INTEREST_MIN,
        "hybrid_current_interest_min": HYBRID_CURRENT_INTEREST_MIN,
        "evergreen_strength_min": EVERGREEN_STRENGTH_MIN,
    }
    normalized["ranking_components"] = _ranking_components(normalized, kind)
    normalized["control_score"] = _control_score(normalized, kind)
    normalized["why"] = _candidate_reasons(normalized, kind)
    if kind == "short":
        normalized["short_admission"] = panel._short_admission(normalized)
        normalized["format_hint"] = "moment"
    return normalized


def _quality_qualified(candidate: dict[str, Any]) -> bool:
    return float(candidate.get("opportunity_score", 0.0) or 0.0) >= MIN_OPPORTUNITY_SCORE


def _is_distinct(candidate: dict[str, Any], chosen: list[dict[str, Any]]) -> bool:
    return not any(
        memory_ui._same_topic_across_formats(
            str(candidate.get("title") or ""), str(prior.get("title") or "")
        )
        for prior in chosen
    )


def _diverse_top(candidates: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    """Admission -> role priority -> composite ranking -> semantic diversity."""
    admitted = [
        item for item in candidates
        if _market_class(item) in SURFACE_MARKET_CLASSES and _quality_qualified(item)
    ]
    ranked = sorted(admitted, key=lambda item: float(item.get("control_score", 0.0) or 0.0), reverse=True)
    chosen: list[dict[str, Any]] = []

    for bucket in SURFACE_MARKET_CLASSES:
        candidate = next(
            (
                item for item in ranked
                if _market_class(item) == bucket and _is_distinct(item, chosen)
            ),
            None,
        )
        if candidate is not None:
            chosen.append(candidate)
        if len(chosen) >= limit:
            return chosen[:limit]

    for item in ranked:
        if item in chosen or not _is_distinct(item, chosen):
            continue
        chosen.append(item)
        if len(chosen) >= limit:
            break
    return chosen[:limit]



def _candidate_panel_text(kind: str, candidates: list[dict[str, Any]]) -> str:
    icon = "🎬" if kind == "long" else "⚡"
    label = "الحلقة" if kind == "long" else "الشورت"
    count = min(3, len(candidates))
    badges = ("1️⃣", "2️⃣", "3️⃣")
    lines = [f"{icon} أفضل الخيارات — {label}", ""]
    if count < 3:
        lines.extend([
            f"وجدت {count} فقط اجتازت حد الجودة {MIN_OPPORTUNITY_SCORE * 10:.1f}/10.",
            "",
        ])
    for index, item in enumerate(candidates[:3]):
        quality = float(item.get("opportunity_score", 0.0) or 0.0) * 10
        trend = float(item.get("trend_score", 0.0) or 0.0) * 10
        lines.append(f"{badges[index]} {str(item.get('title') or '').strip()}")
        lines.append(f"   ⭐ الجودة {quality:.1f}/10 · 📈 الآن {trend:.1f}/10")
        lines.append(f"   {_market_class_ar(item)}")
        why = [str(value) for value in (item.get("why") or []) if str(value).strip()]
        if why:
            lines.append(f"   💡 {why[0]}")
        lines.append("")
    lines.append("اختر الفكرة، أو افتح «التفاصيل» لمعرفة سبب اختيارها. الاختيار لا يبدأ الإنتاج.")
    return "\n".join(lines).strip()


def _candidate_detail(item: dict[str, Any], index: int) -> str:
    quality = float(item.get("opportunity_score", 0.0) or 0.0) * 10
    audience = float(item.get("audience_fit", 0.0) or 0.0) * 10
    evergreen = float(item.get("evergreen_score", 0.0) or 0.0) * 10
    hook = float(item.get("hook_potential", 0.0) or 0.0) * 10
    retention = float(item.get("retention_potential", 0.0) or 0.0) * 10
    packaging = float(item.get("title_thumbnail_potential", 0.0) or 0.0) * 10
    emotional = float(item.get("emotional_pull", 0.0) or 0.0) * 10
    trend = float(item.get("trend_score", 0.0) or 0.0) * 10
    competition = float(item.get("competition_opportunity", 0.0) or 0.0) * 10
    evidence_quality = float(item.get("evidence_quality", 0.0) or 0.0) * 10
    feasibility = float(item.get("production_feasibility", 0.0) or 0.0) * 10
    why = [str(value).strip() for value in (item.get("why") or []) if str(value).strip()]
    evidence_notes = [str(value).strip() for value in (item.get("evidence") or []) if str(value).strip()]
    evidence_items = item.get("market_evidence")
    market = next((value for value in evidence_items or [] if isinstance(value, dict)), {})

    lines = [
        f"🔎 لماذا اختيرت الفكرة {index + 1}؟",
        "",
        str(item.get("title") or ""),
        "",
        f"⭐ التقييم العام: {quality:.1f}/10  ·  الحد المطلوب: {MIN_OPPORTUNITY_SCORE * 10:.1f}/10",
        f"🏷️ {_market_class_ar(item)}",
    ]
    if why:
        lines.append("💡 " + " — ".join(why[:2]))
    lines.extend([
        "",
        "أهم الدرجات:",
        f"🎯 الجمهور {audience:.1f} · 🌲 الاستمرارية {evergreen:.1f} · 🪝 الهوك {hook:.1f}",
        f"⏱️ الاحتفاظ {retention:.1f} · 🖼️ العنوان {packaging:.1f} · ❤️ الجذب {emotional:.1f}",
        f"📈 الآن {trend:.1f} · ⚔️ المنافسة {competition:.1f} · 📚 الدليل {evidence_quality:.1f} · 🎬 الإنتاج {feasibility:.1f}",
    ])
    if evidence_notes:
        lines.extend(["", "لماذا لفتت الفكرة البحث؟"])
        lines.extend(f"• {value[:220]}" for value in evidence_notes[:2])
    if market:
        samples = int(market.get("sample_count", 0) or 0)
        channels = int(market.get("distinct_channels", 0) or 0)
        window = int(market.get("window_days", 30) or 30)
        median = float(market.get("median_views_per_day", 0.0) or 0.0)
        lines.extend([
            "",
            f"📊 YouTube: {samples} عينات من {channels} قنوات خلال {window} يومًا",
            f"الوسيط: {median:,.0f} مشاهدة/يوم",
        ])
    return "\n".join(lines)

def install(*, core: Any, panel: Any) -> None:
    """Bind the ranking policy to both core globals and Telegram presentation hooks."""
    if str(getattr(core, "RESEARCH_CONTRACT_VERSION", "")) != RESEARCH_CONTRACT_VERSION:
        raise RuntimeError("Topic ranking policy contract does not match Topic Research V2")

    for name, value in {
        "STRONG_CURRENT_INTEREST_MIN": STRONG_CURRENT_INTEREST_MIN,
        "HYBRID_CURRENT_INTEREST_MIN": HYBRID_CURRENT_INTEREST_MIN,
        "EVERGREEN_STRENGTH_MIN": EVERGREEN_STRENGTH_MIN,
        "MIN_OPPORTUNITY_SCORE": MIN_OPPORTUNITY_SCORE,
        "_quality_qualified": _quality_qualified,
        "_market_class": _market_class,
        "_market_class_ar": _market_class_ar,
        "_ranking_components": _ranking_components,
        "_control_score": _control_score,
        "_candidate_reasons": _candidate_reasons,
        "_build_candidate_payload": _build_candidate_payload,
        "_diverse_top": _diverse_top,
        "_candidate_panel_text": _candidate_panel_text,
        "_candidate_detail": _candidate_detail,
    }.items():
        setattr(core, name, value)

    previous_reason = core._research_failure_reason
    if not getattr(previous_reason, "_isco_multistage_market_reason", False):
        def reason(exc: Exception) -> str:
            if "distinct production-ready candidate" in str(exc):
                return (
                    "السبب: لم يُعثر على مواضيع مناسبة بهذا البحث بعد فلاتر السوق والجودة "
                    f"(opportunity_score ≥{MIN_OPPORTUNITY_SCORE * 10:.1f}/10). "
                    "جرّب معايير أو seed queries مختلفة."
                )
            return previous_reason(exc)

        setattr(reason, "_isco_multistage_market_reason", True)
        core._research_failure_reason = reason

    panel._build_candidate_payload = _build_candidate_payload
    panel._candidate_panel_text = _candidate_panel_text
    panel._candidate_detail = _candidate_detail
