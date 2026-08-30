from __future__ import annotations

import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import telegram_control_active_ui as active
from scripts import telegram_control_panel as panel
from scripts import telegram_control_simple_ui as simple
from scripts import telegram_topic_memory_ui as memory_ui

RESEARCH_CONTRACT_VERSION = "topic-research-v2"
SEEN_COOLDOWN_DAYS = 21
MIN_LIVE_MARKET_CANDIDATES = 3


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _recent_seen_topics(state: dict[str, Any], kind: str, *, now: datetime | None = None) -> list[str]:
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    cutoff = reference.astimezone(timezone.utc) - timedelta(days=SEEN_COOLDOWN_DAYS)
    result: list[str] = []
    sessions = state.get("sessions")
    if not isinstance(sessions, dict):
        return result
    for session in sessions.values():
        if not isinstance(session, dict) or str(session.get("kind") or "") != kind:
            continue
        created = _parse_time(session.get("created_at"))
        if created is None or created < cutoff:
            continue
        candidates = session.get("candidates")
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            title = str(candidate.get("title") or "").strip()
            if title and not any(memory_ui._same_topic_across_formats(title, prior) for prior in result):
                result.append(title)
    return result[-60:]


def _market_evidence(candidate: dict[str, Any]) -> dict[str, Any]:
    items = candidate.get("market_evidence")
    if not isinstance(items, list):
        return {}
    return next((item for item in items if isinstance(item, dict)), {})


def _market_class(candidate: dict[str, Any]) -> str:
    if str(candidate.get("market_timing_status") or "") != "measured":
        return "unverified"
    trend = float(candidate.get("trend_score", 0.0) or 0.0)
    evergreen = float(candidate.get("evergreen_score", 0.0) or 0.0)
    if trend >= 0.66:
        return "rising"
    if trend >= 0.42 and evergreen >= 0.70:
        return "hybrid"
    if evergreen >= 0.70:
        return "evergreen"
    return "explore"


def _market_class_ar(candidate: dict[str, Any]) -> str:
    return {
        "rising": "🔥 صاعد الآن",
        "hybrid": "⚖️ هجين: الآن + مستمر",
        "evergreen": "🌲 Evergreen مع قياس حي",
        "explore": "🧭 فرصة استكشاف",
        "unverified": "⚪ توقيت السوق غير متحقق",
    }[_market_class(candidate)]


def _control_score(candidate: dict[str, Any], kind: str) -> float:
    channel_fit = float(candidate.get("channel_fit_score", candidate.get("opportunity_score", 0.0)) or 0.0)
    timing = float(candidate.get("trend_score", 0.0) or 0.0)
    evidence = float(candidate.get("evidence_quality", 0.0) or 0.0)
    feasibility = float(candidate.get("production_feasibility", 0.0) or 0.0)
    if kind == "short":
        creative = (
            0.27 * float(candidate.get("hook_potential", 0.0) or 0.0)
            + 0.25 * float(candidate.get("retention_potential", 0.0) or 0.0)
            + 0.16 * float(candidate.get("emotional_pull", 0.0) or 0.0)
            + 0.14 * float(candidate.get("audience_fit", 0.0) or 0.0)
            + 0.10 * feasibility
            + 0.08 * float(candidate.get("title_thumbnail_potential", 0.0) or 0.0)
        )
        return round(0.82 * creative + 0.13 * timing + 0.05 * evidence, 3)
    return round(0.70 * channel_fit + 0.20 * timing + 0.07 * evidence + 0.03 * feasibility, 3)


def _candidate_reasons(candidate: dict[str, Any], kind: str) -> list[str]:
    reasons = ["ملاءمة تحريرية قوية للقناة"]
    timing = float(candidate.get("trend_score", 0.0) or 0.0)
    if timing >= 0.66:
        reasons.append("زخم حديث واضح عبر أكثر من قناة")
    elif timing >= 0.42:
        reasons.append("اهتمام حديث قابل للبناء مع قيمة مستمرة")
    else:
        reasons.append("توقيت سوق مقاس لكن الزخم محدود")
    if kind == "short" and float(candidate.get("hook_potential", 0.0) or 0.0) >= 0.85:
        reasons[0] = "Hook قوي ومباشر للشورت"
    return reasons[:2]


def _build_candidate_payload(candidate: dict[str, Any], kind: str) -> dict[str, Any]:
    normalized = dict(candidate)
    normalized["research_contract_version"] = RESEARCH_CONTRACT_VERSION
    normalized["market_class"] = _market_class(normalized)
    normalized["control_score"] = _control_score(normalized, kind)
    normalized["why"] = _candidate_reasons(normalized, kind)
    if kind == "short":
        normalized["short_admission"] = panel._short_admission(normalized)
        normalized["format_hint"] = "moment"
    return normalized


def _diverse_top(candidates: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    ranked = sorted(candidates, key=lambda item: float(item.get("control_score", 0.0) or 0.0), reverse=True)
    chosen: list[dict[str, Any]] = []
    for bucket in ("rising", "hybrid", "evergreen", "explore"):
        candidate = next(
            (
                item for item in ranked
                if _market_class(item) == bucket
                and not any(
                    memory_ui._same_topic_across_formats(
                        str(item.get("title") or ""), str(prior.get("title") or "")
                    )
                    for prior in chosen
                )
            ),
            None,
        )
        if candidate is not None:
            chosen.append(candidate)
        if len(chosen) >= limit:
            return chosen[:limit]
    for item in ranked:
        if item in chosen:
            continue
        if any(
            memory_ui._same_topic_across_formats(
                str(item.get("title") or ""), str(prior.get("title") or "")
            )
            for prior in chosen
        ):
            continue
        chosen.append(item)
        if len(chosen) >= limit:
            break
    return chosen[:limit]


def _candidate_panel_text(kind: str, candidates: list[dict[str, Any]]) -> str:
    icon = "🎬" if kind == "long" else "⚡"
    heading = "3 فرص بحث حي للحلقة" if kind == "long" else "3 فرص بحث حي للشورت"
    badges = ("1️⃣", "2️⃣", "3️⃣")
    lines = [f"{icon} {heading}", "", "الدرجة تفصل بين ملاءمة القناة وتوقيت السوق؛ «الآن» مبني على YouTube حديث وليس تقديرًا من النموذج.", ""]
    for index, item in enumerate(candidates[:3]):
        score = float(item.get("control_score", 0.0) or 0.0) * 10
        fit = float(item.get("channel_fit_score", 0.0) or 0.0) * 10
        trend = float(item.get("trend_score", 0.0) or 0.0) * 10
        lines.append(f"{badges[index]} {str(item.get('title') or '').strip()}")
        lines.append(f"   ⭐ فرصة: {score:.1f}/10 · ملاءمة القناة: {fit:.1f}/10 · الآن: {trend:.1f}/10")
        lines.append(f"   {_market_class_ar(item)}")
        why = [str(value) for value in (item.get("why") or []) if str(value).strip()]
        if why:
            lines.append("   💡 " + " — ".join(why[:2]))
        lines.append("")
    lines.append("👇 اختر فكرة، أو افتح التفاصيل لرؤية دليل السوق وتاريخه. لا يبدأ Production من هذه البطاقة.")
    return "\n".join(lines).strip()


def _candidate_detail(item: dict[str, Any], index: int) -> str:
    score = float(item.get("control_score", 0.0) or 0.0) * 10
    fit = float(item.get("channel_fit_score", 0.0) or 0.0) * 10
    trend = float(item.get("trend_score", 0.0) or 0.0) * 10
    evidence = _market_evidence(item)
    lines = [
        f"🔎 تفاصيل الفكرة {index + 1}",
        "",
        str(item.get("title") or ""),
        "",
        f"⭐ فرصة مركبة: {score:.1f}/10",
        f"🎯 ملاءمة القناة التحريرية: {fit:.1f}/10",
        f"📈 الاهتمام الحالي المقاس: {trend:.1f}/10",
        f"{_market_class_ar(item)}",
        f"🌲 Evergreen: {float(item.get('evergreen_score', 0.0) or 0.0) * 10:.1f}/10",
        f"🪝 قوة الـHook: {float(item.get('hook_potential', 0.0) or 0.0) * 10:.1f}/10",
        f"⏱️ الاحتفاظ المتوقع: {float(item.get('retention_potential', 0.0) or 0.0) * 10:.1f}/10",
        f"🖼️ العنوان/الصورة: {float(item.get('title_thumbnail_potential', 0.0) or 0.0) * 10:.1f}/10",
        f"🎬 سهولة الإنتاج: {float(item.get('production_feasibility', 0.0) or 0.0) * 10:.1f}/10",
        f"📚 جودة الخلفية البحثية: {float(item.get('evidence_quality', 0.0) or 0.0) * 10:.1f}/10",
        "",
        "📊 دليل السوق الحي:",
        f"• الاستعلام: {str(evidence.get('query') or item.get('market_query') or '')}",
        f"• النافذة: آخر {int(evidence.get('window_days', 30) or 30)} يومًا",
        f"• عينات صالحة: {int(evidence.get('sample_count', 0) or 0)} من {int(evidence.get('distinct_channels', 0) or 0)} قنوات",
        f"• أعلى سرعة مشاهدة: {float(evidence.get('max_views_per_day', 0.0) or 0.0):,.0f} مشاهدة/يوم",
        f"• الوسيط: {float(evidence.get('median_views_per_day', 0.0) or 0.0):,.0f} مشاهدة/يوم",
        f"• قيس في: {str(evidence.get('fetched_at') or item.get('researched_at') or '')[:19].replace('T', ' ')} UTC",
    ]
    top = evidence.get("top_samples")
    if isinstance(top, list) and top:
        lines.extend(["", "أقوى العينات:"])
        for sample in top[:3]:
            if not isinstance(sample, dict):
                continue
            lines.append(
                f"• {str(sample.get('title') or '')[:120]} — {float(sample.get('views_per_day', 0.0) or 0.0):,.0f}/يوم"
            )
    lines.extend(["", "ملاحظة: «ملاءمة القناة» تقييم تحريري، وليست ادعاءً بأنها قراءة مباشرة لـCTR/Retention جمهور القناة."])
    return "\n".join(lines)


def _research_failure_reason(exc: Exception) -> str:
    reason = active._research_failure_reason(exc)
    if reason:
        return reason
    text = str(exc)
    if "Live topic selection failed" in text or "Live topic research" in text:
        return "السبب: لم يكتمل عقد البحث الحي؛ تم منع الـFallback الثابت من الظهور كأنه بحث جديد."
    if "live market" in text.casefold() or "market" in text.casefold():
        return "السبب: لم تتوفر 3 فرص بدليل سوق حي كافٍ؛ لن أعرض رقم «اهتمام حالي» غير موثق."
    return ""


def _research_current_v2(state_path: Path) -> None:
    state = panel.load_state(state_path)
    pending = next(
        (item for item in state["pending_actions"] if isinstance(item, dict) and item.get("status") == "pending"),
        None,
    )
    if pending is None:
        print("No pending research action")
        return
    token = panel._read_secret_file("TELEGRAM_BOT_TOKEN_FILE", required=True)
    client = panel.TelegramClient(token)
    chat_id = pending.get("chat_id")
    kind = str(pending.get("kind") or "long")
    pending["attempts"] = int(pending.get("attempts", 0) or 0) + 1
    try:
        from isco_video_agent.research import gather_signals, measure_market_timing, select_topic

        gemini = (os.environ.get("GEMINI_API_KEY") or "").strip()
        youtube = (os.environ.get("YOUTUBE_API_KEY") or "").strip()
        model = (os.environ.get("GEMINI_CONTENT_MODEL") or "gemini-2.5-flash").strip()
        region = (os.environ.get("YOUTUBE_REGION") or "SA").strip()
        language = (os.environ.get("YOUTUBE_LANGUAGE") or "ar").strip()
        exclusions = _recent_seen_topics(state, kind)
        signals = gather_signals(
            gemini,
            youtube,
            (os.environ.get("TRENDS_GEO") or "SA").strip(),
            region,
            language,
        )
        _, ranked = select_topic(
            gemini,
            signals,
            model,
            allow_fallback=False,
            excluded_topics=exclusions,
        )
        ranked = measure_market_timing(youtube, ranked, region=region, language=language)
        live = [item for item in ranked if item.market_timing_status == "measured" and item.source_mode == "live_research"]
        if len(live) < MIN_LIVE_MARKET_CANDIDATES:
            raise RuntimeError(
                f"Live market evidence produced only {len(live)} candidates; need {MIN_LIVE_MARKET_CANDIDATES}"
            )
        candidates = [_build_candidate_payload(item.to_dict(), kind) for item in live]
        candidates.sort(key=lambda item: float(item.get("control_score", 0.0) or 0.0), reverse=True)
        unused_candidates, used_filtered = active._filter_used_candidates(state, kind, candidates)
        if kind == "long":
            research_ready = simple._research_ready_long_candidates(gemini, unused_candidates[:6], model)
        else:
            research_ready = unused_candidates
        chosen = _diverse_top(research_ready, 3)
        if len(chosen) != 3:
            raise RuntimeError("Live research did not produce three distinct production-ready candidates")
        session_id = secrets.token_hex(4)
        session = {
            "schema_version": 2,
            "session_id": session_id,
            "kind": kind,
            "created_at": panel._now(),
            "research_contract_version": RESEARCH_CONTRACT_VERSION,
            "source_mode": "live_research",
            "seen_cooldown_days": SEEN_COOLDOWN_DAYS,
            "excluded_recent_topics": exclusions,
            "candidates": chosen,
            "used_topics_filtered": used_filtered,
        }
        state["sessions"][session_id] = session
        active._archive_session_candidates(state, session, review_candidates=unused_candidates)
        state[active.ACTIVE_RESEARCH_SESSION_KEY] = session_id
        state.pop(active.PRODUCTION_TARGET_KEY, None)
        pending["status"] = "completed"
        pending["completed_at"] = panel._now()
    except Exception as exc:
        active._clear_current_selection(state)
        reason = _research_failure_reason(exc)
        if pending["attempts"] >= 3:
            pending["status"] = "failed"
            pending["failed_at"] = panel._now()
            text = (
                "تعذر إكمال البحث الحي بعد عدة محاولات. لم يبدأ أي إنتاج، ولم أعرض مخزون Evergreen ثابتًا كأنه نتيجة جديدة. "
                "يمكنك طلب البحث مرة أخرى من اللوحة."
            )
            if reason:
                text += "\n" + reason
            client.send(chat_id, text, keyboard=panel._main_keyboard())
        else:
            text = (
                "لم يكتمل البحث الحي في هذه الدورة وسأبقي الطلب قيد المحاولة تلقائيًا خلال دقائق. "
                "لن أعرض FallBack ثابتًا أو رقم اهتمام حالي غير موثق. لم يبدأ أي إنتاج."
            )
            if reason:
                text += "\n" + reason
            client.send(
                chat_id,
                text,
                keyboard=[[{"text": "🧭 الحالة", "callback_data": "cmd:status"}]],
            )
        panel.save_state(state_path, state)
        raise

    state["pending_actions"] = [
        item for item in state["pending_actions"]
        if not (isinstance(item, dict) and item.get("status") in {"completed", "failed"})
    ]
    state["last_event_at"] = panel._now()
    panel.save_state(state_path, state)
    try:
        client.send(chat_id, _candidate_panel_text(kind, chosen), keyboard=panel._candidate_keyboard(session_id, kind))
    except Exception as exc:
        print(f"Research V2 completed and saved, but notifying chat {chat_id} failed: {exc}")


def install_v2() -> None:
    panel._build_candidate_payload = _build_candidate_payload
    panel._candidate_panel_text = _candidate_panel_text
    panel._candidate_detail = _candidate_detail
    panel.research = _research_current_v2


def main() -> None:
    memory_ui._install_policy()
    from scripts import telegram_canonical_status_bridge as canonical_status_bridge
    from scripts import telegram_persistent_control_ui as persistent_ui
    from scripts import telegram_rich_integration as rich_integration

    persistent_ui.install()
    memory_ui._install_library_split()
    memory_ui._install_choice_clarity()
    canonical_status_bridge.install()
    rich_integration.install()
    active._install()
    install_v2()
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    memory_ui._require_poll_identity(mode)
    panel.main()


if __name__ == "__main__":
    main()
