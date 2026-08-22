from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_SCHEMA_VERSION = 1
REQUEST_SCHEMA_VERSION = 1
SESSION_TTL_HOURS = 24
MAX_PENDING_RESEARCH = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_secret_file(name: str, *, required: bool = False) -> str:
    path = (os.environ.get(name) or "").strip()
    if not path:
        if required:
            raise RuntimeError(f"{name} is not configured")
        return ""
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        if required:
            raise RuntimeError(f"{name} could not be read") from exc
        return ""
    if required and not value:
        raise RuntimeError(f"{name} is empty")
    return value


def _github_output(name: str, value: str) -> None:
    target = (os.environ.get("GITHUB_OUTPUT") or "").strip()
    if not target:
        print(f"{name}={value}")
        return
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def _new_state() -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "telegram_offset": 0,
        "sessions": {},
        "requests": {},
        "pending_actions": [],
        "last_event_at": None,
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return _new_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("Control-panel state is invalid JSON") from exc
    if not isinstance(data, dict) or data.get("schema_version") != STATE_SCHEMA_VERSION:
        raise RuntimeError("Unsupported control-panel state schema")
    for name, default in (("sessions", {}), ("requests", {}), ("pending_actions", [])):
        if not isinstance(data.get(name), type(default)):
            raise RuntimeError(f"Control-panel state field is malformed: {name}")
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _canonical_hash(document: dict[str, Any]) -> str:
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class TelegramClient:
    def __init__(self, token: str):
        if not token:
            raise RuntimeError("Telegram token is empty")
        self.token = token

    def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        encoded = urllib.parse.urlencode(
            {
                key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
                for key, value in (payload or {}).items()
            }
        ).encode("utf-8")
        request = urllib.request.Request(url, data=encoded)
        with urllib.request.urlopen(request, timeout=35) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {body.get('description', 'unknown error')}")
        return body.get("result")

    def send(self, chat_id: int | str, text: str, *, keyboard: list[list[dict[str, str]]] | None = None) -> Any:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        return self.call("sendMessage", payload)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        self.call("answerCallbackQuery", payload)


class GitHubReleaseClient:
    def __init__(self, repository: str, token: str = ""):
        self.repository = repository
        self.token = token

    def _get(self, url: str) -> Any:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "isco-telegram-control"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.loads(response.read().decode("utf-8"))

    def releases(self) -> list[dict[str, Any]]:
        data = self._get(f"https://api.github.com/repos/{self.repository}/releases?per_page=30")
        return data if isinstance(data, list) else []

    def latest(self, prefix: str | None = None) -> dict[str, Any] | None:
        for release in self.releases():
            if not isinstance(release, dict) or release.get("draft"):
                continue
            tag = str(release.get("tag_name") or "")
            if prefix is None or tag.startswith(prefix):
                return release
        return None

    def asset_json(self, release: dict[str, Any], name: str) -> dict[str, Any] | None:
        for asset in release.get("assets", []) or []:
            if not isinstance(asset, dict) or asset.get("name") != name:
                continue
            url = str(asset.get("browser_download_url") or "")
            if not url:
                return None
            try:
                data = self._get(url)
            except Exception:
                return None
            return data if isinstance(data, dict) else None
        return None


def _main_keyboard() -> list[list[dict[str, str]]]:
    return [
        [
            {"text": "💡 اقتراح حلقة", "callback_data": "cmd:topic"},
            {"text": "⚡ اقتراح شورت", "callback_data": "cmd:short"},
        ],
        [
            {"text": "🎬 آخر فيديو", "callback_data": "cmd:last_long"},
            {"text": "📱 آخر شورت", "callback_data": "cmd:last_short"},
        ],
        [
            {"text": "📦 آخر تسليم", "callback_data": "cmd:last_delivery"},
            {"text": "🧭 الحالة", "callback_data": "cmd:status"},
        ],
    ]


def _menu_text() -> str:
    locked = (os.environ.get("CONTROL_PLANE_PRODUCTION_ENABLED") or "false").strip().lower() != "true"
    state = "🔒 الإنتاج مقفول حاليًا" if locked else "🟢 الإنتاج مفعّل"
    return (
        "🎛 لوحة نداء اليقظة\n\n"
        "اختر ما تريد فقط، وسأعرض التفاصيل عند طلبها.\n"
        f"{state}\n\n"
        "النشر وإعدادات YouTube النهائية تبقى يدويًا في YouTube Studio."
    )


def _normalize_command(text: str) -> str:
    return " ".join(str(text or "").strip().casefold().split())


def _command_kind(text: str) -> str | None:
    value = _normalize_command(text)
    if value in {"/start", "/menu", "menu", "القائمة", "لوحة", "لوحة التحكم", "ابدأ"}:
        return "menu"
    if value in {"أريد موضوع", "اريد موضوع", "موضوع", "موضوع حلقة", "حلقة", "حلقة جديدة", "اقترح موضوع"}:
        return "topic"
    if value in {"أريد شورت", "اريد شورت", "شورت", "موضوع شورت", "شورت جديد", "اقترح شورت"}:
        return "short"
    if value in {"آخر فيديو", "اخر فيديو", "الفيديو الأخير", "الفيديو الاخير"}:
        return "last_long"
    if value in {"آخر شورت", "اخر شورت", "الشورت الأخير", "الشورت الاخير"}:
        return "last_short"
    if value in {"آخر تسليم", "اخر تسليم", "التسليم", "الحزمة"}:
        return "last_delivery"
    if value in {"الحالة", "حالة", "status", "/status"}:
        return "status"
    return None


def _authorized_user(update: dict[str, Any], allowed_user_id: int, allowed_chat_id: str) -> tuple[bool, int | str | None, int | None]:
    callback = update.get("callback_query") if isinstance(update, dict) else None
    if isinstance(callback, dict):
        actor = callback.get("from") or {}
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        user_id = actor.get("id")
        chat_id = chat.get("id")
    else:
        message = update.get("message") if isinstance(update, dict) else None
        message = message if isinstance(message, dict) else {}
        actor = message.get("from") or {}
        chat = message.get("chat") or {}
        user_id = actor.get("id")
        chat_id = chat.get("id")
    try:
        parsed_user = int(user_id)
    except (TypeError, ValueError):
        return False, chat_id, None
    if parsed_user != allowed_user_id:
        return False, chat_id, parsed_user
    if allowed_chat_id and str(chat_id) != str(allowed_chat_id):
        return False, chat_id, parsed_user
    return True, chat_id, parsed_user


def _queue_research(state: dict[str, Any], kind: str, chat_id: int | str) -> bool:
    pending = state["pending_actions"]
    if any(item.get("kind") == kind and item.get("status") == "pending" for item in pending if isinstance(item, dict)):
        return False
    if sum(1 for item in pending if isinstance(item, dict) and item.get("status") == "pending") >= MAX_PENDING_RESEARCH:
        return False
    pending.append(
        {
            "action_id": secrets.token_hex(4),
            "kind": kind,
            "chat_id": str(chat_id),
            "requested_at": _now(),
            "attempts": 0,
            "status": "pending",
        }
    )
    return True


def _release_keyboard(release: dict[str, Any], *, packaging: bool) -> list[list[dict[str, str]]]:
    rows: list[list[dict[str, str]]] = []
    url = str(release.get("html_url") or "")
    if url:
        rows.append([{"text": "📦 فتح الحزمة", "url": url}])
    if packaging:
        tag = str(release.get("tag_name") or "")
        if tag and len(tag) <= 35:
            rows.append([{"text": "🅰️ عناوين وصور A/B/C", "callback_data": f"pack:{tag}"}])
    rows.append([{"text": "↩️ اللوحة", "callback_data": "cmd:menu"}])
    return rows


def _format_release(release: dict[str, Any] | None, kind: str) -> str:
    if release is None:
        label = "فيديو" if kind == "long" else "شورت"
        return f"لا يوجد {label} جاهز في Releases حتى الآن."
    assets = [str(item.get("name") or "") for item in release.get("assets", []) if isinstance(item, dict)]
    tag = str(release.get("tag_name") or "")
    published = str(release.get("published_at") or release.get("created_at") or "")[:10]
    has_video = "final.mp4" in assets or any(name.endswith(".mp4") for name in assets)
    package = "✅ حزمة جاهزة" if has_video else "⚠️ حزمة بلا MP4 واضح"
    icon = "🎬" if kind == "long" else "📱"
    return f"{icon} آخر {'فيديو' if kind == 'long' else 'شورت'}\n\n{package}\n🏷 {tag}\n📅 {published or 'غير معروف'}"


def _status_text(state: dict[str, Any], releases: GitHubReleaseClient) -> str:
    enabled = (os.environ.get("CONTROL_PLANE_PRODUCTION_ENABLED") or "false").strip().lower() == "true"
    approved = [item for item in state["requests"].values() if isinstance(item, dict) and str(item.get("status", "")).startswith("approved")]
    last = approved[-1] if approved else None
    long_release = releases.latest("video-")
    short_release = releases.latest("short-")
    lines = ["🧭 حالة النظام", "", "🟢 لوحة Telegram: جاهزة", "🟢 البحث والاختيار: جاهزان"]
    lines.append("🟢 إنتاج Control Plane: مفعّل" if enabled else "🔒 إنتاج Control Plane: مقفول — لا Run تلقائي")
    lines.append(f"🎬 آخر فيديو: {str((long_release or {}).get('tag_name') or 'لا يوجد')}")
    lines.append(f"📱 آخر شورت: {str((short_release or {}).get('tag_name') or 'لا يوجد')}")
    if last:
        lines.extend(["", "✅ آخر قرار معتمد:", str(last.get("approved_topic") or "")[:180], f"الحالة: {last.get('status')}"])
    return "\n".join(lines)


def _candidate_score(candidate: dict[str, Any], kind: str) -> float:
    if kind == "short":
        weights = {
            "hook_potential": 0.23,
            "retention_potential": 0.22,
            "emotional_pull": 0.15,
            "audience_fit": 0.15,
            "title_thumbnail_potential": 0.10,
            "production_feasibility": 0.10,
            "evidence_quality": 0.05,
        }
        return round(sum(float(candidate.get(name, 0.0) or 0.0) * weight for name, weight in weights.items()), 3)
    return round(float(candidate.get("opportunity_score", 0.0) or 0.0), 3)


def _candidate_reasons(candidate: dict[str, Any], kind: str) -> list[str]:
    labels = {
        "audience_fit": "ملاءمة قوية لجمهور القناة",
        "retention_potential": "قابلية جيدة للاحتفاظ",
        "hook_potential": "Hook واضح وقابل للبناء",
        "title_thumbnail_potential": "فرصة قوية للعنوان والصورة",
        "evergreen_score": "قيمة مستمرة وليست مؤقتة",
        "trend_score": "له إشارة اهتمام حالية",
        "emotional_pull": "شد عاطفي مناسب",
        "production_feasibility": "قابل للتنفيذ بصريًا بجودة جيدة",
        "evidence_quality": "الدليل البحثي أقوى نسبيًا",
    }
    fields = list(labels)
    if kind == "short":
        fields = ["hook_potential", "retention_potential", "emotional_pull", "audience_fit", "production_feasibility", "title_thumbnail_potential"]
    ranked = sorted(fields, key=lambda name: float(candidate.get(name, 0.0) or 0.0), reverse=True)
    return [labels[name] for name in ranked[:2]]


def _single_action_for(candidate: dict[str, Any]) -> str:
    pillar = str(candidate.get("pillar") or "understand")
    if pillar == "rise":
        return "اختر خطوة واحدة صغيرة قابلة للتنفيذ وابدأ بها اليوم"
    if pillar == "see":
        return "أعد النظر في موقف واحد اليوم من الزاوية الجديدة"
    return "لاحظ موقفًا واحدًا اليوم واسأل ما الذي يحرّكه فعلًا"


def _short_admission(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "knowledge_gap_score": round(float(candidate.get("hook_potential", 0.0) or 0.0) * 10, 2),
        "reframe_score": round(max(float(candidate.get("emotional_pull", 0.0) or 0.0), float(candidate.get("audience_fit", 0.0) or 0.0)) * 10, 2),
        "immediate_action_score": round(max(0.7, float(candidate.get("production_feasibility", 0.0) or 0.0)) * 10, 2),
        "short_fit_score": round(_candidate_score(candidate, "short") * 10, 2),
        "single_action_contract": _single_action_for(candidate),
    }


def _build_candidate_payload(candidate: dict[str, Any], kind: str) -> dict[str, Any]:
    normalized = dict(candidate)
    normalized["control_score"] = _candidate_score(candidate, kind)
    normalized["why"] = _candidate_reasons(candidate, kind)
    if kind == "short":
        normalized["short_admission"] = _short_admission(candidate)
        normalized["format_hint"] = "moment"
    return normalized


def _candidate_panel_text(kind: str, candidates: list[dict[str, Any]]) -> str:
    icon = "🎬" if kind == "long" else "⚡"
    heading = "3 مواضيع مقترحة للحلقة" if kind == "long" else "3 مواضيع مقترحة للشورت"
    lines = [f"{icon} {heading}", ""]
    for index, item in enumerate(candidates, 1):
        score = float(item.get("control_score", 0.0) or 0.0) * 10
        why = item.get("why") or []
        lines.append(f"{index}) {item.get('title', '')}")
        lines.append(f"   فرصة: {score:.1f}/10")
        if why:
            lines.append("   • " + " — ".join(str(x) for x in why[:2]))
        lines.append("")
    lines.append("اضغط اختيار، أو التفاصيل إذا أردت رؤية الدليل والدرجات.")
    return "\n".join(lines).strip()


def _candidate_keyboard(session_id: str, kind: str) -> list[list[dict[str, str]]]:
    rows = []
    for index in range(3):
        pick = "pickshort" if kind == "short" else "pick"
        rows.append(
            [
                {"text": f"✅ اختيار {index + 1}", "callback_data": f"{pick}:{session_id}:{index}"},
                {"text": f"🔎 تفاصيل {index + 1}", "callback_data": f"detail:{session_id}:{index}"},
            ]
        )
    rows.append([{"text": "🔄 3 مواضيع غيرها", "callback_data": f"refresh:{kind}"}])
    rows.append([{"text": "↩️ اللوحة", "callback_data": "cmd:menu"}])
    return rows


def _candidate_detail(item: dict[str, Any], index: int) -> str:
    score_names = (
        ("audience_fit", "ملاءمة الجمهور"),
        ("hook_potential", "قوة الـHook"),
        ("retention_potential", "الاحتفاظ"),
        ("title_thumbnail_potential", "العنوان/الصورة"),
        ("evergreen_score", "Evergreen"),
        ("trend_score", "الاهتمام الحالي"),
        ("production_feasibility", "سهولة الإنتاج"),
        ("evidence_quality", "جودة الدليل"),
    )
    lines = [f"🔎 تفاصيل الخيار {index + 1}", "", str(item.get("title") or ""), ""]
    for key, label in score_names:
        value = float(item.get(key, 0.0) or 0.0) * 10
        lines.append(f"{label}: {value:.1f}/10")
    evidence = [str(x).strip() for x in item.get("evidence", []) if str(x).strip()]
    if evidence:
        lines.extend(["", "أهم الدليل:"])
        lines.extend(f"• {value[:260]}" for value in evidence[:3])
    return "\n".join(lines)


def _session(state: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    value = state["sessions"].get(session_id)
    return value if isinstance(value, dict) else None


def _approve(state: dict[str, Any], session: dict[str, Any], index: int, scope: str) -> dict[str, Any]:
    candidates = session.get("candidates") or []
    if not isinstance(candidates, list) or not 0 <= index < len(candidates):
        raise RuntimeError("Candidate selection is stale or invalid")
    candidate = candidates[index]
    if not isinstance(candidate, dict):
        raise RuntimeError("Candidate selection is malformed")
    request_id = "req-" + secrets.token_hex(6)
    kind = str(session.get("kind") or "long")
    approved_scope = "short_only" if kind == "short" else ("long_plus_sibling_shorts" if scope == "bundle" else "long_only")
    request: dict[str, Any] = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "request_id": request_id,
        "source": "telegram_editorial_control_panel",
        "kind": kind,
        "approval_scope": approved_scope,
        "approved_by_user": True,
        "approved_at": _now(),
        "approved_topic": str(candidate.get("title") or "").strip(),
        "format": "moment" if kind == "short" else str(candidate.get("format_hint") or "film"),
        "weekly_option_id": f"telegram:{session.get('session_id')}:{index + 1}",
        "research_pack": [str(x)[:500] for x in candidate.get("evidence", [])[:6]],
        "content_boundaries": [],
        "candidate": candidate,
        "short_admission": candidate.get("short_admission") if kind == "short" else None,
        "sibling_shorts": {"mode": "auto_distinct", "minimum": 2, "maximum": 3} if approved_scope == "long_plus_sibling_shorts" else None,
        "production_dispatch_authorized": False,
        "status": "approved_waiting_production_activation",
    }
    request["request_sha256"] = _canonical_hash(request)
    state["requests"][request_id] = request
    state["last_event_at"] = _now()
    return request


def _approval_text(request: dict[str, Any]) -> str:
    if request["approval_scope"] == "long_plus_sibling_shorts":
        scope = "حلقة طويلة + 2–3 Shorts مختلفة حسب المادة"
    elif request["approval_scope"] == "short_only":
        scope = "Short فقط"
    else:
        scope = "حلقة طويلة فقط"
    return (
        "✅ تم اعتماد القرار وحفظه\n\n"
        f"الموضوع: {request['approved_topic']}\n"
        f"النطاق: {scope}\n"
        f"رقم الطلب: {request['request_id']}\n\n"
        "🔒 لم يبدأ أي Production Run. التفعيل مقفول حاليًا كما طلبت."
    )


def _show_packaging(client: TelegramClient, releases: GitHubReleaseClient, chat_id: int | str, tag: str) -> None:
    release = next((item for item in releases.releases() if str(item.get("tag_name") or "") == tag), None)
    if not release:
        client.send(chat_id, "لم أجد هذه الحزمة بعد الآن.", keyboard=_main_keyboard())
        return
    plan = releases.asset_json(release, "thumbnail-plan.json")
    if not plan:
        client.send(chat_id, "هذه الحزمة لا تحتوي thumbnail-plan.json قابلًا للقراءة.", keyboard=_release_keyboard(release, packaging=False))
        return
    candidates = plan.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        client.send(chat_id, "لا توجد خيارات A/B/C صالحة في الحزمة.", keyboard=_release_keyboard(release, packaging=False))
        return
    lines = ["🅰️ عناوين وصور A/B/C", ""]
    rows: list[list[dict[str, str]]] = []
    assets = {str(item.get("name") or ""): str(item.get("browser_download_url") or "") for item in release.get("assets", []) if isinstance(item, dict)}
    for index, item in enumerate(candidates[:3]):
        if not isinstance(item, dict):
            continue
        slot = str(item.get("experiment_slot") or chr(ord("A") + index))
        lines.append(f"{slot} — {str(item.get('title_ar') or '')}")
        thumb_text = str(item.get("text_ar") or "").strip()
        if thumb_text:
            lines.append(f"الصورة: «{thumb_text}»")
        reason = str(item.get("why_it_can_work") or "").strip()
        if reason:
            lines.append(f"لماذا: {reason[:220]}")
        lines.append("")
        file_name = str(item.get("file") or f"thumbnail-{index + 1}.jpg")
        if assets.get(file_name):
            rows.append([{"text": f"🖼 فتح صورة {slot}", "url": assets[file_name]}])
    rows.extend(_release_keyboard(release, packaging=False))
    client.send(chat_id, "\n".join(lines).strip(), keyboard=rows)


def _handle_command(kind: str, client: TelegramClient, state: dict[str, Any], releases: GitHubReleaseClient, chat_id: int | str) -> None:
    if kind == "menu":
        client.send(chat_id, _menu_text(), keyboard=_main_keyboard())
        return
    if kind in {"topic", "short"}:
        queued = _queue_research(state, "long" if kind == "topic" else "short", chat_id)
        client.send(
            chat_id,
            "🔎 أبحث الآن عن أفضل 3 فرص مناسبة للقناة..." if queued else "يوجد طلب بحث من نفس النوع قيد التنفيذ بالفعل.",
            keyboard=[[{"text": "↩️ اللوحة", "callback_data": "cmd:menu"}]],
        )
        return
    if kind == "status":
        client.send(chat_id, _status_text(state, releases), keyboard=_main_keyboard())
        return
    if kind in {"last_long", "last_short"}:
        release = releases.latest("video-" if kind == "last_long" else "short-")
        client.send(chat_id, _format_release(release, "long" if kind == "last_long" else "short"), keyboard=_release_keyboard(release, packaging=True) if release else _main_keyboard())
        return
    if kind == "last_delivery":
        release = releases.latest(None)
        if release and not (str(release.get("tag_name") or "").startswith("video-") or str(release.get("tag_name") or "").startswith("short-")):
            release = next((item for item in releases.releases() if str(item.get("tag_name") or "").startswith(("video-", "short-"))), None)
        if not release:
            client.send(chat_id, "لا توجد حزمة تسليم بعد.", keyboard=_main_keyboard())
            return
        tag = str(release.get("tag_name") or "")
        label = "long" if tag.startswith("video-") else "short"
        client.send(chat_id, "📦 آخر تسليم\n\n" + _format_release(release, label), keyboard=_release_keyboard(release, packaging=True))
        return
    client.send(chat_id, _menu_text(), keyboard=_main_keyboard())


def poll(state_path: Path) -> None:
    state = load_state(state_path)
    token = _read_secret_file("TELEGRAM_BOT_TOKEN_FILE")
    allowed_text = _read_secret_file("TELEGRAM_ALLOWED_USER_ID_FILE")
    allowed_chat = _read_secret_file("TELEGRAM_CHAT_ID_FILE")
    if not token or not allowed_text:
        print("Telegram editorial control disabled: bot token or allowed user is missing")
        _github_output("needs_engine", "false")
        save_state(state_path, state)
        return
    try:
        allowed_user = int(allowed_text)
    except ValueError as exc:
        raise RuntimeError("TELEGRAM_ALLOWED_USER_ID_FILE is invalid") from exc

    client = TelegramClient(token)
    repository = (os.environ.get("GITHUB_REPOSITORY") or "mymusa79-tech/Isco-Video-Runner").strip()
    releases = GitHubReleaseClient(repository, (os.environ.get("GITHUB_TOKEN") or "").strip())
    offset = int(state.get("telegram_offset", 0) or 0)
    updates = client.call("getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["message", "callback_query"]}) or []
    if not isinstance(updates, list):
        updates = []

    for update in updates:
        if not isinstance(update, dict) or "update_id" not in update:
            continue
        state["telegram_offset"] = max(int(state.get("telegram_offset", 0) or 0), int(update["update_id"]) + 1)
        authorized, chat_id, _ = _authorized_user(update, allowed_user, allowed_chat)
        callback = update.get("callback_query")
        if not authorized:
            if isinstance(callback, dict) and callback.get("id"):
                try:
                    client.answer_callback(str(callback["id"]), "غير مصرح")
                except Exception:
                    pass
            continue
        if chat_id is None:
            continue

        if isinstance(callback, dict):
            callback_id = str(callback.get("id") or "")
            data = str(callback.get("data") or "")
            if callback_id:
                client.answer_callback(callback_id)
            parts = data.split(":")
            if len(parts) >= 2 and parts[0] == "cmd":
                _handle_command(parts[1], client, state, releases, chat_id)
            elif len(parts) == 2 and parts[0] == "refresh" and parts[1] in {"long", "short"}:
                _handle_command("topic" if parts[1] == "long" else "short", client, state, releases, chat_id)
            elif len(parts) == 2 and parts[0] == "pack":
                _show_packaging(client, releases, chat_id, parts[1])
            elif len(parts) == 3 and parts[0] == "detail":
                session = _session(state, parts[1])
                if not session:
                    client.send(chat_id, "انتهت صلاحية هذه الخيارات. اطلب 3 مواضيع جديدة.", keyboard=_main_keyboard())
                    continue
                try:
                    index = int(parts[2])
                    item = session["candidates"][index]
                except Exception:
                    client.send(chat_id, "الخيار غير صالح.", keyboard=_main_keyboard())
                    continue
                pick = "pickshort" if session.get("kind") == "short" else "pick"
                client.send(chat_id, _candidate_detail(item, index), keyboard=[[{"text": f"✅ اختيار {index + 1}", "callback_data": f"{pick}:{parts[1]}:{index}"}], [{"text": "↩️ الخيارات", "callback_data": "cmd:menu"}]])
            elif len(parts) == 3 and parts[0] == "pickshort":
                session = _session(state, parts[1])
                if not session or session.get("kind") != "short":
                    client.send(chat_id, "انتهت صلاحية هذا الاختيار.", keyboard=_main_keyboard())
                    continue
                request = _approve(state, session, int(parts[2]), "short")
                client.send(chat_id, _approval_text(request), keyboard=_main_keyboard())
            elif len(parts) == 3 and parts[0] == "pick":
                session = _session(state, parts[1])
                if not session or session.get("kind") != "long":
                    client.send(chat_id, "انتهت صلاحية هذا الاختيار.", keyboard=_main_keyboard())
                    continue
                index = int(parts[2])
                candidate = session["candidates"][index]
                client.send(
                    chat_id,
                    f"اختر نطاق الإنتاج لهذا الموضوع:\n\n{candidate.get('title', '')}",
                    keyboard=[
                        [{"text": "🎬 حلقة + Shorts", "callback_data": f"scope:{parts[1]}:{index}:bundle"}],
                        [{"text": "🎬 حلقة فقط", "callback_data": f"scope:{parts[1]}:{index}:long"}],
                        [{"text": "↩️ اللوحة", "callback_data": "cmd:menu"}],
                    ],
                )
            elif len(parts) == 4 and parts[0] == "scope" and parts[3] in {"bundle", "long"}:
                session = _session(state, parts[1])
                if not session or session.get("kind") != "long":
                    client.send(chat_id, "انتهت صلاحية هذا الاختيار.", keyboard=_main_keyboard())
                    continue
                request = _approve(state, session, int(parts[2]), parts[3])
                client.send(chat_id, _approval_text(request), keyboard=_main_keyboard())
            else:
                client.send(chat_id, _menu_text(), keyboard=_main_keyboard())
        else:
            message = update.get("message") or {}
            command = _command_kind(str(message.get("text") or ""))
            _handle_command(command or "menu", client, state, releases, chat_id)
        state["last_event_at"] = _now()

    save_state(state_path, state)
    needs_engine = any(isinstance(item, dict) and item.get("status") == "pending" for item in state["pending_actions"])
    _github_output("needs_engine", "true" if needs_engine else "false")


def research(state_path: Path) -> None:
    state = load_state(state_path)
    pending = next((item for item in state["pending_actions"] if isinstance(item, dict) and item.get("status") == "pending"), None)
    if pending is None:
        print("No pending research action")
        return
    token = _read_secret_file("TELEGRAM_BOT_TOKEN_FILE", required=True)
    client = TelegramClient(token)
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
        candidates = [_build_candidate_payload(item.to_dict(), kind) for item in ranked]
        candidates.sort(key=lambda item: float(item.get("control_score", 0.0) or 0.0), reverse=True)
        chosen = candidates[:3]
        if len(chosen) != 3:
            raise RuntimeError("Research did not produce three usable candidates")
        session_id = secrets.token_hex(4)
        state["sessions"][session_id] = {
            "schema_version": 1,
            "session_id": session_id,
            "kind": kind,
            "created_at": _now(),
            "candidates": chosen,
        }
        pending["status"] = "completed"
        pending["completed_at"] = _now()
        client.send(chat_id, _candidate_panel_text(kind, chosen), keyboard=_candidate_keyboard(session_id, kind))
    except Exception as exc:
        if pending["attempts"] >= 3:
            pending["status"] = "failed"
            pending["failed_at"] = _now()
            client.send(chat_id, "تعذر إكمال البحث بعد عدة محاولات. لم يبدأ أي إنتاج. يمكنك طلب البحث مرة أخرى من اللوحة.", keyboard=_main_keyboard())
        else:
            client.send(chat_id, "تعذر البحث في هذه الدورة وسأبقي الطلب قيد المحاولة. لم يبدأ أي إنتاج.", keyboard=[[{"text": "🧭 الحالة", "callback_data": "cmd:status"}]])
        save_state(state_path, state)
        raise

    state["pending_actions"] = [item for item in state["pending_actions"] if not (isinstance(item, dict) and item.get("status") in {"completed", "failed"})]
    state["last_event_at"] = _now()
    save_state(state_path, state)


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram editorial control plane for Isco Video Runner")
    parser.add_argument("mode", choices=("poll", "research"))
    parser.add_argument("--state", required=True, type=Path)
    args = parser.parse_args()
    if args.mode == "poll":
        poll(args.state)
    else:
        research(args.state)


if __name__ == "__main__":
    main()
