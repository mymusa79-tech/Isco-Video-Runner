from __future__ import annotations

import json
import os
import re
import secrets
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from scripts import telegram_control_panel as panel
from scripts import telegram_control_simple_ui as simple
from scripts.telegram_production_queue import enqueue_request, pending_dispatch

_BASE_POLL = panel.poll
PRODUCTION_TARGET_KEY = "production_target"
ACTIVE_RESEARCH_SESSION_KEY = "active_research_session_id"
SAVED_SUGGESTIONS_KEY = "saved_suggestions"
USED_TOPICS_KEY = "used_topics"
MAX_SAVED_SUGGESTIONS = 30
MAX_USED_TOPICS = 1000
SAVED_PAGE_SIZE = 5
USED_PAGE_SIZE = 5

# Conservative automatic-review policy. A saved idea is never deleted because of
# one or two missed searches. Long and Short libraries are reviewed independently.
SAVED_WEAK_SCORE = 0.55
SAVED_WEAK_MIN_AGE_DAYS = 21
SAVED_WEAK_MISSED_REVIEWS = 3
SAVED_MARGINAL_SCORE = 0.70
SAVED_MARGINAL_MIN_AGE_DAYS = 60
SAVED_MARGINAL_MISSED_REVIEWS = 6
SAVED_HARD_STALE_DAYS = 180
SAVED_HARD_STALE_MISSED_REVIEWS = 10

_TOPIC_STOPWORDS = {
    "كيف", "لماذا", "هل", "ماذا", "ما", "من", "في", "على", "عن", "الى", "إلى",
    "ان", "أن", "هذا", "هذه", "ذلك", "تلك", "عندما", "حين", "الذي", "التي",
}


def _production_enabled() -> bool:
    return (os.environ.get("CONTROL_PLANE_PRODUCTION_ENABLED") or "false").strip().lower() == "true"


def _main_keyboard() -> list[list[dict[str, str]]]:
    rows = list(simple._main_keyboard())
    rows.insert(
        1,
        [
            {"text": "📚 محفوظة", "callback_data": "cmd:saved"},
            {"text": "✅ مستعملة", "callback_data": "cmd:used"},
        ],
    )
    if _production_enabled():
        rows.insert(2, [{"text": "🚀 ابدأ الإنتاج المعتمد", "callback_data": "cmd:produce_latest"}])
    return rows


def _menu_text() -> str:
    if _production_enabled():
        production = (
            "🟢 إنتاج Telegram مفعّل.\n"
            "لا يبدأ شيء بمجرد اعتماد الموضوع؛ التشغيل يحتاج ضغطة مستقلة على «🚀 ابدأ الإنتاج المعتمد»."
        )
    else:
        production = "🔒 إنتاج Telegram مقفول حاليًا."
    return (
        "🎛 نداء اليقظة\n\n"
        "اختر ما تحتاجه فقط. التفاصيل تظهر عند طلبها.\n"
        "📚 غير المختار يبقى محفوظًا ويُراجع مع كل بحث جديد من نفس النوع.\n"
        "✅ الموضوع الذي يكتمل إنتاجه ينتقل إلى سجل المستعملة ولا يعود في بحث النوع نفسه.\n\n"
        f"{production}\n\n"
        "🔒 لا نشر إلى YouTube من هذه اللوحة.\n"
        "الرفع والنشر والجدولة تبقى يدويًا بيدك داخل YouTube Studio."
    )


def _command_kind(text: str) -> str | None:
    value = panel._normalize_command(text)
    if value in {"المحفوظة", "اقتراحات محفوظة", "الاقتراحات المحفوظة", "المواضيع المحفوظة"}:
        return "saved"
    if value in {"المستعملة", "المواضيع المستعملة", "مواضيع مستعملة", "المنتجة", "المواضيع المنتجة"}:
        return "used"
    return simple._command_kind(text)


def _approval_text(request: dict[str, Any]) -> str:
    if request.get("approval_scope") == "long_plus_sibling_shorts":
        scope = "حلقة طويلة + 2–3 Shorts مختلفة حسب المادة"
    elif request.get("approval_scope") in {"short_only", "short_sibling"}:
        scope = "Short فقط"
    else:
        scope = "حلقة طويلة فقط"
    if _production_enabled():
        action = (
            "لم يبدأ الإنتاج بعد.\n"
            "إذا كان القرار نهائيًا اضغط «🚀 ابدأ الإنتاج المعتمد» من اللوحة."
        )
    else:
        action = "🔒 لم يبدأ أي Production Run. التفعيل مقفول حاليًا."
    return (
        "✅ تم اعتماد القرار وحفظه\n\n"
        f"الموضوع: {request.get('approved_topic', '')}\n"
        f"النطاق: {scope}\n"
        f"رقم الطلب: {request.get('request_id', '')}\n\n"
        f"{action}"
    )


def _clear_current_selection(state: dict[str, Any]) -> None:
    state.pop(PRODUCTION_TARGET_KEY, None)
    state.pop(ACTIVE_RESEARCH_SESSION_KEY, None)


def _current_target(state: dict[str, Any]) -> dict[str, str] | None:
    target = state.get(PRODUCTION_TARGET_KEY)
    if not isinstance(target, dict):
        return None
    request_id = str(target.get("request_id") or "").strip()
    request_sha256 = str(target.get("request_sha256") or "").strip()
    session_id = str(target.get("session_id") or "").strip()
    active_session = str(state.get(ACTIVE_RESEARCH_SESSION_KEY) or "").strip()
    if not request_id or not request_sha256 or not session_id or session_id != active_session:
        return None
    return {"request_id": request_id, "request_sha256": request_sha256, "session_id": session_id}


def _saved_store(state: dict[str, Any]) -> list[dict[str, Any]]:
    value = state.setdefault(SAVED_SUGGESTIONS_KEY, [])
    if not isinstance(value, list):
        raise RuntimeError("Telegram saved-suggestions state is malformed")
    return value


def _used_store(state: dict[str, Any]) -> list[dict[str, Any]]:
    value = state.setdefault(USED_TOPICS_KEY, [])
    if not isinstance(value, list):
        raise RuntimeError("Telegram used-topics state is malformed")
    return value


def _candidate_copy(candidate: dict[str, Any]) -> dict[str, Any]:
    copied = json.loads(json.dumps(candidate, ensure_ascii=False))
    if not isinstance(copied, dict):
        raise RuntimeError("Saved Telegram candidate could not be copied")
    return copied


def _normalize_title(title: str) -> str:
    value = unicodedata.normalize("NFKC", str(title or "")).casefold().replace("ـ", "")
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    value = value.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي"}))
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def _suggestion_key(kind: str, title: str) -> str:
    return f"{kind}|{_normalize_title(title)}"


def _topic_tokens(title: str) -> list[str]:
    return [token for token in _normalize_title(title).split() if token not in _TOPIC_STOPWORDS and len(token) > 1]


def _same_topic_title(left: str, right: str) -> bool:
    a = _normalize_title(left)
    b = _normalize_title(right)
    if not a or not b:
        return False
    if a == b:
        return True
    ta = set(_topic_tokens(a))
    tb = set(_topic_tokens(b))
    if min(len(ta), len(tb)) < 3:
        return False
    common = len(ta & tb)
    containment = common / min(len(ta), len(tb))
    union = len(ta | tb)
    jaccard = common / union if union else 0.0
    sequence = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return containment >= 0.85 and jaccard >= 0.70 and sequence >= 0.78


def _candidate_score(candidate: dict[str, Any]) -> float:
    raw = candidate.get("control_score")
    if raw is None:
        raw = candidate.get("opportunity_score")
    try:
        value = float(raw or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    return max(0.0, min(1.0, value))


def _timestamp(value: object) -> datetime | None:
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


def _age_days(item: dict[str, Any], now: datetime) -> int | None:
    saved = _timestamp(item.get("saved_at")) or _timestamp(item.get("last_seen_at"))
    if saved is None:
        return None
    return max(0, int((now - saved).total_seconds() // 86400))


def _available_saved(state: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for item in _saved_store(state):
        if not isinstance(item, dict) or item.get("status") != "available":
            continue
        candidate = item.get("candidate")
        if not isinstance(candidate, dict) or not str(candidate.get("title") or "").strip():
            continue
        items.append(item)
    return sorted(
        items,
        key=lambda item: (str(item.get("last_seen_at") or item.get("saved_at") or ""), str(item.get("archive_id") or "")),
        reverse=True,
    )


def _used_topics(state: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for item in _used_store(state):
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "") not in {"long", "short"}:
            continue
        if not str(item.get("topic") or "").strip() or not str(item.get("request_id") or "").strip():
            continue
        items.append(item)
    return sorted(items, key=lambda item: (str(item.get("used_at") or ""), str(item.get("request_id") or "")), reverse=True)


def _is_used_topic(state: dict[str, Any], kind: str, title: str) -> bool:
    for item in _used_topics(state):
        if str(item.get("kind") or "") != kind:
            continue
        if _same_topic_title(title, str(item.get("topic") or "")):
            return True
    return False


def _filter_used_candidates(state: dict[str, Any], kind: str, candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    kept: list[dict[str, Any]] = []
    blocked = 0
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        title = str(candidate.get("title") or "").strip()
        if not title:
            continue
        if _is_used_topic(state, kind, title):
            blocked += 1
            continue
        kept.append(candidate)
    return kept, blocked


def _mark_request_used(
    state: dict[str, Any],
    request: dict[str, Any],
    *,
    release_tag: str,
    used_at: str | None = None,
) -> dict[str, Any]:
    request_id = str(request.get("request_id") or "").strip()
    kind = str(request.get("kind") or "").strip()
    topic = str(request.get("approved_topic") or "").strip()
    release_tag = str(release_tag or "").strip()
    if not request_id or kind not in {"long", "short"} or not topic or not release_tag:
        raise RuntimeError("Completed Telegram request cannot be recorded as a used topic")
    used_at = used_at or panel._now()
    if _timestamp(used_at) is None:
        raise RuntimeError("Used-topic timestamp is invalid")
    store = _used_store(state)
    existing = next(
        (item for item in store if isinstance(item, dict) and str(item.get("request_id") or "") == request_id),
        None,
    )
    record = {
        "schema_version": 1,
        "request_id": request_id,
        "kind": kind,
        "topic": topic,
        "dedupe_key": _suggestion_key(kind, topic),
        "approval_scope": str(request.get("approval_scope") or ""),
        "release_tag": release_tag,
        "used_at": used_at,
    }
    if existing is None:
        store.append(record)
    else:
        existing.clear()
        existing.update(record)
        record = existing
    # Any stale saved copy of a successfully produced topic must disappear.
    state[SAVED_SUGGESTIONS_KEY] = [
        item
        for item in _saved_store(state)
        if not (
            isinstance(item, dict)
            and str(item.get("kind") or "") == kind
            and isinstance(item.get("candidate"), dict)
            and _same_topic_title(str(item["candidate"].get("title") or ""), topic)
        )
    ]
    ordered = _used_topics(state)
    keep_ids = {str(item.get("request_id") or "") for item in ordered[:MAX_USED_TOPICS]}
    state[USED_TOPICS_KEY] = [
        item for item in _used_store(state) if isinstance(item, dict) and str(item.get("request_id") or "") in keep_ids
    ]
    return record


def _retention_rank(item: dict[str, Any]) -> tuple[float, int, str, str]:
    candidate = item.get("candidate") if isinstance(item.get("candidate"), dict) else {}
    misses = int(item.get("missed_reviews", 0) or 0)
    return (
        _candidate_score(candidate),
        -misses,
        str(item.get("last_seen_at") or item.get("saved_at") or ""),
        str(item.get("archive_id") or ""),
    )


def _prune_saved(state: dict[str, Any], *, protected_ids: set[str] | None = None) -> None:
    available = _available_saved(state)
    protected = set(protected_ids or set())
    protected_items = [item for item in available if str(item.get("archive_id") or "") in protected]
    other_items = [item for item in available if str(item.get("archive_id") or "") not in protected]
    protected_items.sort(key=_retention_rank, reverse=True)
    other_items.sort(key=_retention_rank, reverse=True)
    keep = protected_items[:MAX_SAVED_SUGGESTIONS]
    if len(keep) < MAX_SAVED_SUGGESTIONS:
        keep.extend(other_items[: MAX_SAVED_SUGGESTIONS - len(keep)])
    keep_ids = {str(item.get("archive_id") or "") for item in keep}
    state[SAVED_SUGGESTIONS_KEY] = [
        item
        for item in _saved_store(state)
        if isinstance(item, dict)
        and item.get("status") == "available"
        and str(item.get("archive_id") or "") in keep_ids
    ]


def _automatic_prune_reason(item: dict[str, Any], now: datetime) -> str | None:
    candidate = item.get("candidate")
    if not isinstance(candidate, dict) or not str(candidate.get("title") or "").strip():
        return "malformed"
    age = _age_days(item, now)
    misses = int(item.get("missed_reviews", 0) or 0)
    score = _candidate_score(candidate)
    if age is not None and age >= SAVED_HARD_STALE_DAYS and misses >= SAVED_HARD_STALE_MISSED_REVIEWS:
        return "hard_stale"
    if age is not None and age >= SAVED_WEAK_MIN_AGE_DAYS and misses >= SAVED_WEAK_MISSED_REVIEWS and score < SAVED_WEAK_SCORE:
        return "weak_and_repeatedly_absent"
    if age is not None and age >= SAVED_MARGINAL_MIN_AGE_DAYS and misses >= SAVED_MARGINAL_MISSED_REVIEWS and score < SAVED_MARGINAL_SCORE:
        return "marginal_and_stale"
    return None


def _review_saved_suggestions(
    state: dict[str, Any],
    kind: str,
    fresh_candidates: list[dict[str, Any]],
    *,
    now_text: str | None = None,
) -> dict[str, Any]:
    if kind not in {"long", "short"}:
        raise RuntimeError("Saved-suggestion review kind is unsupported")
    now_text = now_text or panel._now()
    now = _timestamp(now_text)
    if now is None:
        raise RuntimeError("Saved-suggestion review timestamp is invalid")
    fresh_by_key = {
        _suggestion_key(kind, str(candidate.get("title") or "")): candidate
        for candidate in fresh_candidates
        if isinstance(candidate, dict) and str(candidate.get("title") or "").strip()
    }
    kept: list[dict[str, Any]] = []
    report: dict[str, Any] = {"kind": kind, "reviewed_at": now_text, "reviewed": 0, "refreshed": 0, "kept": 0, "pruned": []}
    for item in _saved_store(state):
        if not isinstance(item, dict) or item.get("status") != "available":
            continue
        candidate = item.get("candidate")
        item_kind = str(item.get("kind") or "")
        title = str(candidate.get("title") or "").strip() if isinstance(candidate, dict) else ""
        if item_kind not in {"long", "short"} or not title:
            report["pruned"].append({"archive_id": str(item.get("archive_id") or ""), "reason": "malformed"})
            continue
        if _is_used_topic(state, item_kind, title):
            report["pruned"].append({"archive_id": str(item.get("archive_id") or ""), "title": title[:180], "reason": "already_used"})
            continue
        if item_kind != kind:
            kept.append(item)
            continue
        report["reviewed"] += 1
        item["review_count"] = int(item.get("review_count", 0) or 0) + 1
        item["last_reviewed_at"] = now_text
        key = str(item.get("dedupe_key") or _suggestion_key(item_kind, title))
        item["dedupe_key"] = key
        if key in fresh_by_key:
            item["candidate"] = _candidate_copy(fresh_by_key[key])
            item["missed_reviews"] = 0
            item["last_seen_at"] = now_text
            report["refreshed"] += 1
            kept.append(item)
            continue
        item["missed_reviews"] = int(item.get("missed_reviews", 0) or 0) + 1
        reason = _automatic_prune_reason(item, now)
        if reason is not None:
            report["pruned"].append({"archive_id": str(item.get("archive_id") or ""), "title": title[:180], "reason": reason})
            continue
        kept.append(item)
    state[SAVED_SUGGESTIONS_KEY] = kept
    report["kept"] = sum(
        1 for item in kept if isinstance(item, dict) and item.get("status") == "available" and str(item.get("kind") or "") == kind
    )
    return report


def _archive_session_candidates(
    state: dict[str, Any],
    session: dict[str, Any],
    *,
    review_candidates: list[dict[str, Any]] | None = None,
) -> list[str]:
    candidates = session.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise RuntimeError("Telegram research session has no candidates to save")
    if not all(isinstance(candidate, dict) for candidate in candidates):
        raise RuntimeError("Telegram research candidate is malformed")
    kind = str(session.get("kind") or "long")
    session_id = str(session.get("session_id") or "").strip()
    if kind not in {"long", "short"} or not session_id:
        raise RuntimeError("Telegram research session cannot be archived")
    now = panel._now()
    session["saved_review"] = _review_saved_suggestions(
        state,
        kind,
        review_candidates if review_candidates is not None else candidates,
        now_text=now,
    )
    store = _saved_store(state)
    ids: list[str] = []
    for candidate in candidates:
        title = str(candidate.get("title") or "").strip()
        if not title:
            raise RuntimeError("Telegram research candidate has no title")
        if _is_used_topic(state, kind, title):
            raise RuntimeError("Research attempted to archive an already used topic")
        key = _suggestion_key(kind, title)
        existing = next(
            (
                item for item in store
                if isinstance(item, dict) and item.get("status") == "available" and item.get("dedupe_key") == key
            ),
            None,
        )
        if existing is None:
            existing = {
                "schema_version": 1,
                "archive_id": secrets.token_hex(4),
                "status": "available",
                "kind": kind,
                "dedupe_key": key,
                "saved_at": now,
                "review_count": 0,
            }
            store.append(existing)
        existing["candidate"] = _candidate_copy(candidate)
        existing["source_session_id"] = session_id
        existing["last_seen_at"] = now
        existing["last_reviewed_at"] = now
        existing["missed_reviews"] = 0
        ids.append(str(existing["archive_id"]))
    session["saved_suggestion_ids"] = ids
    _prune_saved(state, protected_ids=set(ids))
    live_ids = {str(item.get("archive_id") or "") for item in _available_saved(state)}
    session["saved_suggestion_ids"] = [archive_id for archive_id in ids if archive_id in live_ids]
    return list(session["saved_suggestion_ids"])


def _consume_saved_candidate(state: dict[str, Any], session: dict[str, Any], index: int) -> None:
    ids = session.get("saved_suggestion_ids")
    if not isinstance(ids, list) or not 0 <= index < len(ids):
        return
    archive_id = str(ids[index] or "")
    if not archive_id:
        return
    state[SAVED_SUGGESTIONS_KEY] = [
        item for item in _saved_store(state)
        if not (isinstance(item, dict) and str(item.get("archive_id") or "") == archive_id)
    ]


def _saved_page(state: dict[str, Any], page: int) -> tuple[str, list[list[dict[str, str]]]]:
    items = _available_saved(state)
    if not items:
        return (
            "📚 الاقتراحات المحفوظة\n\nلا توجد أفكار محفوظة حاليًا. عندما أقدم لك 3 اقتراحات، أي فكرة لا تختارها ستبقى هنا.",
            [[{"text": "✨ اقترح", "callback_data": "cmd:suggest"}], [{"text": "↩️ اللوحة", "callback_data": "cmd:menu"}]],
        )
    pages = max(1, (len(items) + SAVED_PAGE_SIZE - 1) // SAVED_PAGE_SIZE)
    page = min(max(0, page), pages - 1)
    start = page * SAVED_PAGE_SIZE
    current = items[start : start + SAVED_PAGE_SIZE]
    lines = [
        "📚 الاقتراحات المحفوظة", "",
        f"{len(items)} فكرة غير مختارة محفوظة — صفحة {page + 1}/{pages}.",
        "تُراجع تلقائيًا مع كل بحث جديد من نفس النوع. اختيار فكرة هنا لا يبدأ الإنتاج؛ ستراها أولًا ثم تؤكدها بالطريقة المعتادة.",
    ]
    rows: list[list[dict[str, str]]] = []
    for item in current:
        candidate = item["candidate"]
        title = str(candidate.get("title") or "").strip()
        icon = "🎬" if item.get("kind") == "long" else "📱"
        short_title = title if len(title) <= 42 else title[:39].rstrip() + "…"
        rows.append([{"text": f"{icon} {short_title}", "callback_data": f"cmd:savedpick-{item['archive_id']}"}])
    nav: list[dict[str, str]] = []
    if page > 0:
        nav.append({"text": "⬅️ أحدث", "callback_data": f"cmd:savedpage-{page - 1}"})
    if page + 1 < pages:
        nav.append({"text": "أقدم ➡️", "callback_data": f"cmd:savedpage-{page + 1}"})
    if nav:
        rows.append(nav)
    rows.append([{"text": "↩️ اللوحة", "callback_data": "cmd:menu"}])
    return "\n".join(lines), rows


def _used_page(state: dict[str, Any], page: int) -> tuple[str, list[list[dict[str, str]]]]:
    items = _used_topics(state)
    if not items:
        return (
            "✅ المواضيع المستعملة\n\nلا يوجد موضوع مكتمل الإنتاج في السجل حتى الآن.",
            [[{"text": "↩️ اللوحة", "callback_data": "cmd:menu"}]],
        )
    pages = max(1, (len(items) + USED_PAGE_SIZE - 1) // USED_PAGE_SIZE)
    page = min(max(0, page), pages - 1)
    start = page * USED_PAGE_SIZE
    current = items[start : start + USED_PAGE_SIZE]
    lines = ["✅ المواضيع المستعملة", "", f"{len(items)} موضوعًا مكتمل الإنتاج — صفحة {page + 1}/{pages}.", "هذه القائمة للقراءة فقط وتمنع إعادة الموضوع في بحث النوع نفسه.", ""]
    for index, item in enumerate(current, start + 1):
        icon = "🎬" if item.get("kind") == "long" else "📱"
        lines.append(f"{index}) {icon} {item.get('topic', '')}")
        date = str(item.get("used_at") or "")[:10]
        if date:
            lines.append(f"   {date}")
    rows: list[list[dict[str, str]]] = []
    nav: list[dict[str, str]] = []
    if page > 0:
        nav.append({"text": "⬅️ أحدث", "callback_data": f"cmd:usedpage-{page - 1}"})
    if page + 1 < pages:
        nav.append({"text": "أقدم ➡️", "callback_data": f"cmd:usedpage-{page + 1}"})
    if nav:
        rows.append(nav)
    rows.append([{"text": "↩️ اللوحة", "callback_data": "cmd:menu"}])
    return "\n".join(lines), rows


def _find_saved(state: dict[str, Any], archive_id: str) -> dict[str, Any] | None:
    for item in _available_saved(state):
        if str(item.get("archive_id") or "") == archive_id:
            return item
    return None


def _has_pending_research(state: dict[str, Any]) -> bool:
    return any(isinstance(item, dict) and item.get("status") == "pending" for item in state.get("pending_actions", []))


def _activate_saved_suggestion(state: dict[str, Any], archive_id: str) -> dict[str, Any]:
    if _has_pending_research(state):
        raise RuntimeError("Wait for the current Telegram research to finish before selecting a saved suggestion")
    item = _find_saved(state, archive_id)
    if item is None:
        raise RuntimeError("Saved Telegram suggestion is unavailable")
    candidate = item.get("candidate")
    if not isinstance(candidate, dict):
        raise RuntimeError("Saved Telegram suggestion candidate is malformed")
    kind = str(item.get("kind") or "")
    if kind not in {"long", "short"}:
        raise RuntimeError("Saved Telegram suggestion kind is unsupported")
    title = str(candidate.get("title") or "").strip()
    if _is_used_topic(state, kind, title):
        state[SAVED_SUGGESTIONS_KEY] = [
            saved for saved in _saved_store(state)
            if not (isinstance(saved, dict) and str(saved.get("archive_id") or "") == archive_id)
        ]
        raise RuntimeError("Saved Telegram suggestion was already used")
    _clear_current_selection(state)
    session_id = secrets.token_hex(4)
    session = {
        "schema_version": 1,
        "session_id": session_id,
        "kind": kind,
        "created_at": panel._now(),
        "source": "saved_suggestion",
        "candidates": [_candidate_copy(candidate)],
        "saved_suggestion_ids": [archive_id],
    }
    state.setdefault("sessions", {})[session_id] = session
    state[ACTIVE_RESEARCH_SESSION_KEY] = session_id
    state["last_event_at"] = panel._now()
    return session


def _approve_current(state: dict[str, Any], session: dict[str, Any], index: int, scope: str) -> dict[str, Any]:
    session_id = str(session.get("session_id") or "").strip()
    active_session = str(state.get(ACTIVE_RESEARCH_SESSION_KEY) or "").strip()
    if not session_id or session_id != active_session:
        raise RuntimeError("This Telegram selection is not from the current research session")
    request = simple._approve(state, session, index, scope)
    _consume_saved_candidate(state, session, index)
    state[PRODUCTION_TARGET_KEY] = {
        "request_id": str(request.get("request_id") or ""),
        "request_sha256": str(request.get("request_sha256") or ""),
        "session_id": session_id,
        "selected_at": str(request.get("approved_at") or panel._now()),
    }
    return request


def _handle_command(kind, client, state, releases, chat_id) -> None:
    if kind in {"topic", "short"}:
        _clear_current_selection(state)
        simple._handle_command(kind, client, state, releases, chat_id)
        return
    if kind == "saved" or (isinstance(kind, str) and kind.startswith("savedpage-")):
        page = 0
        if isinstance(kind, str) and kind.startswith("savedpage-"):
            try:
                page = int(kind.removeprefix("savedpage-"))
            except ValueError:
                page = 0
        text, keyboard = _saved_page(state, page)
        client.send(chat_id, text, keyboard=keyboard)
        return
    if kind == "used" or (isinstance(kind, str) and kind.startswith("usedpage-")):
        page = 0
        if isinstance(kind, str) and kind.startswith("usedpage-"):
            try:
                page = int(kind.removeprefix("usedpage-"))
            except ValueError:
                page = 0
        text, keyboard = _used_page(state, page)
        client.send(chat_id, text, keyboard=keyboard)
        return
    if isinstance(kind, str) and kind.startswith("savedpick-"):
        archive_id = kind.removeprefix("savedpick-").strip()
        try:
            session = _activate_saved_suggestion(state, archive_id)
        except RuntimeError as exc:
            message = str(exc)
            if "research to finish" in message:
                text = "⏳ يوجد بحث جديد قيد التنفيذ الآن. انتظر انتهاءه أولًا حتى لا يتغير اختيارك المحفوظ أثناء العمل."
            elif "already used" in message:
                text = "✅ هذه الفكرة أُنتجت بالفعل ونُقلت إلى قائمة المستعملة."
            else:
                text = "هذه الفكرة المحفوظة لم تعد متاحة. افتح القائمة من جديد."
            client.send(chat_id, text, keyboard=_main_keyboard())
            return
        candidate = session["candidates"][0]
        pick = "pickshort" if session.get("kind") == "short" else "pick"
        text = "📚 فكرة محفوظة\n\n" + panel._candidate_detail(candidate, 0)
        client.send(
            chat_id,
            text,
            keyboard=[
                [{"text": "✅ استخدام هذا الموضوع", "callback_data": f"{pick}:{session['session_id']}:0"}],
                [{"text": "↩️ المحفوظة", "callback_data": "cmd:saved"}],
            ],
        )
        return
    if kind == "produce_latest":
        if not _production_enabled():
            client.send(chat_id, "🔒 إنتاج Telegram مقفول حاليًا.", keyboard=_main_keyboard())
            return
        target = _current_target(state)
        if target is None:
            client.send(
                chat_id,
                "لا يوجد اختيار صالح من جلسة الاختيار الحالية. اطلب 3 خيارات جديدة أو اختر فكرة من «📚 محفوظة»، ثم أكدها قبل بدء الإنتاج.",
                keyboard=_main_keyboard(),
            )
            return
        status, action = enqueue_request(state, target["request_id"], target["request_sha256"], chat_id=chat_id)
        if status == "no_ready_request":
            state.pop(PRODUCTION_TARGET_KEY, None)
            text = "الاختيار الحالي لم يعد صالحًا للإنتاج. اختر موضوعًا من جديد ثم أكده."
        elif status == "already_queued":
            text = "⏳ طلب الإنتاج المعتمد موجود بالفعل في طابور الإرسال. لن أكرر التشغيل."
        elif status == "already_reserved_recent":
            text = "🔒 تم حجز هذا الطلب للتشغيل بالفعل. لن أعيد إرساله تلقائيًا؛ أي محاولة لاحقة تحتاج إجراءً صريحًا جديدًا."
        elif status == "already_dispatched_recent":
            text = "✅ تم إرسال هذا الطلب للإنتاج بالفعل مؤخرًا. لن أنشئ محاولة مكررة."
        elif status == "retry_queued":
            text = "🚀 أعدت وضع الاختيار الحالي في طابور التشغيل بعد انتهاء نافذة الحماية من التكرار."
        else:
            text = "🚀 تم تأكيد بدء الإنتاج للاختيار الحالي.\nسيُحجز القرار أولًا داخل الحالة المشفرة، ثم يُرسل مرة واحدة إلى مسار الإنتاج المحمي."
        if action is not None:
            text += f"\n\nرقم الطلب: {action.get('request_id')}"
        text += "\n\nYouTube: النشر يبقى يدويًا فقط."
        client.send(chat_id, text, keyboard=_main_keyboard())
        return
    if kind == "status":
        text = panel._status_text(state, releases)
        text += f"\n📚 محفوظة: {len(_available_saved(state))}"
        text += f"\n✅ مستعملة: {len(_used_topics(state))}"
        text += "\n\n🟢 إنتاج Telegram: مفعّل بضغطة تشغيل مستقلة" if _production_enabled() else "\n\n🔒 إنتاج Telegram: مقفول"
        text += "\n🔒 نشر YouTube: يدوي فقط"
        client.send(chat_id, text, keyboard=_main_keyboard())
        return
    simple._handle_command(kind, client, state, releases, chat_id)


def _research_current(state_path: Path) -> None:
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
        from isco_video_agent.research import gather_signals, select_topic

        gemini = (os.environ.get("GEMINI_API_KEY") or "").strip()
        youtube = (os.environ.get("YOUTUBE_API_KEY") or "").strip()
        model = (os.environ.get("GEMINI_CONTENT_MODEL") or "gemini-2.5-flash").strip()
        signals = gather_signals(
            gemini,
            youtube,
            (os.environ.get("TRENDS_GEO") or "SA").strip(),
            (os.environ.get("YOUTUBE_REGION") or "SA").strip(),
            (os.environ.get("YOUTUBE_LANGUAGE") or "ar").strip(),
        )
        _, ranked = select_topic(gemini, signals, model, allow_fallback=True)
        candidates = [panel._build_candidate_payload(item.to_dict(), kind) for item in ranked]
        candidates.sort(key=lambda item: float(item.get("control_score", 0.0) or 0.0), reverse=True)
        unused_candidates, used_filtered = _filter_used_candidates(state, kind, candidates)
        if kind == "long":
            production_ready = simple._research_ready_long_candidates(gemini, unused_candidates[:5], model)
        else:
            production_ready = unused_candidates
        chosen = production_ready[:3]
        if len(chosen) != 3:
            raise RuntimeError("Research did not produce three unused production-ready candidates")
        session_id = secrets.token_hex(4)
        session = {
            "schema_version": 1,
            "session_id": session_id,
            "kind": kind,
            "created_at": panel._now(),
            "candidates": chosen,
            "used_topics_filtered": used_filtered,
        }
        state["sessions"][session_id] = session
        _archive_session_candidates(state, session, review_candidates=unused_candidates)
        state[ACTIVE_RESEARCH_SESSION_KEY] = session_id
        state.pop(PRODUCTION_TARGET_KEY, None)
        pending["status"] = "completed"
        pending["completed_at"] = panel._now()
        client.send(chat_id, panel._candidate_panel_text(kind, chosen), keyboard=panel._candidate_keyboard(session_id, kind))
    except Exception:
        _clear_current_selection(state)
        if pending["attempts"] >= 3:
            pending["status"] = "failed"
            pending["failed_at"] = panel._now()
            client.send(
                chat_id,
                "تعذر إكمال البحث بعد عدة محاولات. لم يبدأ أي إنتاج. يمكنك طلب البحث مرة أخرى من اللوحة.",
                keyboard=_main_keyboard(),
            )
        else:
            client.send(
                chat_id,
                "تعذر البحث في هذه الدورة وسأبقي الطلب قيد المحاولة. لم يبدأ أي إنتاج.",
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


def _poll(state_path: Path) -> None:
    _BASE_POLL(state_path)
    state = panel.load_state(state_path)
    action = pending_dispatch(state) if _production_enabled() else None
    panel._github_output("needs_production", "true" if action is not None else "false")
    panel._github_output("production_request_id", str(action.get("request_id") or "") if action else "")
    panel._github_output("production_request_sha256", str(action.get("request_sha256") or "") if action else "")
    panel._github_output("production_release_tag", str(action.get("release_tag") or "") if action else "")


def _install() -> None:
    simple._install()
    panel._main_keyboard = _main_keyboard
    panel._menu_text = _menu_text
    panel._command_kind = _command_kind
    panel._approval_text = _approval_text
    panel._approve = _approve_current
    panel._handle_command = _handle_command
    panel.research = _research_current
    panel.poll = _poll


def main() -> None:
    _install()
    panel.main()


if __name__ == "__main__":
    main()
