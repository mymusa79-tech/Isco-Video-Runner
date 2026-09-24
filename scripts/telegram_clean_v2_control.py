from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import secrets
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

STATE_VERSION = 1
CONFIRM_TEXT = "تأكيد الإنتاج"
SCOPES = {"long", "bundle", "short"}
FORMATS_BY_SCOPE = {
    "long": ["film"],
    "bundle": ["film", "short"],
    "short": ["short"],
}
MODEL = os.environ.get("GEMINI_CONTENT_MODEL", "gemini-3.7-flash")
YOUTUBE_REGION = os.environ.get("YOUTUBE_REGION", "SA")
YOUTUBE_LANGUAGE = os.environ.get("YOUTUBE_LANGUAGE", "ar")
WINDOW_DAYS = 30
SHORT_MAX_SECONDS = 30
YOUTUBE_CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID", "UC_fmWGRen6QUQNd4Dj80MgA")
OMAN_OFFSET = timedelta(hours=4)

FALLBACK_IDEAS = [
    ("لماذا نؤجل الأشياء المهمة رغم أننا نعرف قيمتها؟", "التسويف وتأجيل المهام المهمة"),
    ("كيف تستعيد تركيزك بعد أيام من التشتت؟", "استعادة التركيز بعد التشتت"),
    ("لماذا نفقد الحماس بعد بداية قوية؟", "فقدان الحماس بعد بداية قوية"),
    ("كيف تبني عادة تستمر عندما يختفي الدافع؟", "بناء العادات بدون دافع"),
    ("لماذا نشعر أننا متأخرون عن الآخرين؟", "الشعور بالتأخر مقارنة الآخرين"),
    ("كيف تتعامل مع يوم لم تنجز فيه شيئًا؟", "التعامل مع يوم بدون إنجاز"),
    ("متى تتحول الراحة إلى هروب؟", "الراحة والهروب من المسؤوليات"),
    ("كيف تبدأ من جديد دون خطة مثالية؟", "البدء من جديد بدون خطة مثالية"),
    ("لماذا تجعلنا كثرة الخيارات أقل حسمًا؟", "كثرة الخيارات وصعوبة القرار"),
    ("كيف تنهي ما بدأت بدل مطاردة بداية جديدة؟", "إنهاء المشاريع وعدم التشتت"),
    ("كيف تعرف أنك تتقدم حتى لو كان التغيير بطيئًا؟", "علامات التقدم الشخصي البطيء"),
    ("لماذا ننتظر الشعور المناسب قبل أن نتحرك؟", "انتظار الدافع قبل العمل"),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_state() -> dict[str, Any]:
    return {
        "schema_version": STATE_VERSION,
        "ideas": [],
        "sessions": {},
        "requests": {},
        "current_request_id": None,
        "youtube_snapshots": [],
        "updated_at": None,
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_state()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != STATE_VERSION:
        raise RuntimeError("unsupported Telegram Clean V2 state")
    for key, factory in (("ideas", list), ("sessions", dict), ("requests", dict)):
        if not isinstance(data.get(key), factory):
            raise RuntimeError(f"malformed state field: {key}")
    if "youtube_snapshots" not in data:
        data["youtube_snapshots"] = []
    if not isinstance(data.get("youtube_snapshots"), list):
        raise RuntimeError("malformed state field: youtube_snapshots")
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def normalize_title(value: str) -> str:
    text = str(value or "").casefold().strip()
    text = text.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ؤ": "و", "ئ": "ي"}))
    text = re.sub(r"[^\w\u0600-\u06ff]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _topic_key(token: str) -> str:
    value = normalize_title(token)
    if value.startswith("ال") and len(value) > 4:
        value = value[2:]
    for suffix in ("كما", "هما", "كم", "كن", "هم", "هن", "ها", "نا", "ك", "ه", "ي"):
        if value.endswith(suffix) and len(value) - len(suffix) >= 3:
            value = value[: -len(suffix)]
            break
    if value[:1] in {"ن", "ي", "ت"} and len(value) > 4:
        value = value[1:]
    skeleton = "".join(ch for ch in value if ch not in {"ا", "و", "ي"})
    return skeleton if len(skeleton) >= 2 else value


def same_topic(left: str, right: str) -> bool:
    a, b = normalize_title(left), normalize_title(right)
    if not a or not b:
        return False
    if a == b:
        return True
    ta = {_topic_key(token) for token in a.split() if _topic_key(token)}
    tb = {_topic_key(token) for token in b.split() if _topic_key(token)}
    if min(len(ta), len(tb)) < 3:
        return False
    common = len(ta & tb)
    return common >= 3 and common / min(len(ta), len(tb)) >= 0.75 and common / len(ta | tb) >= 0.55


def _json_request(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None, timeout: int = 20) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"User-Agent": "Isco-Clean-V2-Telegram/1", **(headers or {})},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("remote API returned malformed JSON")
    return value


def send_telegram(text: str, keyboard: list[list[dict[str, str]]] | None = None) -> None:
    token = str(os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = str(os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        print("Telegram reply skipped: missing bot token/chat id")
        return
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": str(text)[:3900],
        "disable_web_page_preview": True,
    }
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    try:
        _json_request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            method="POST",
            payload=payload,
            headers={"Content-Type": "application/json"},
            timeout=12,
        )
    except Exception as exc:
        print(f"Telegram reply failed: {type(exc).__name__}")



def _parse_duration_seconds(value: str) -> int:
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
        str(value or ""),
    )
    if not match:
        return 0
    parts = {key: int(raw or 0) for key, raw in match.groupdict().items()}
    return (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


def _latest_by_clean_v2_format(videos: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    last_short = next(
        (
            item
            for item in videos
            if 0 < int(item.get("duration_seconds") or 0) <= SHORT_MAX_SECONDS
        ),
        None,
    )
    last_long = next(
        (
            item
            for item in videos
            if int(item.get("duration_seconds") or 0) > SHORT_MAX_SECONDS
        ),
        None,
    )
    return last_short, last_long


def _youtube_api(resource: str, params: dict[str, str]) -> dict[str, Any]:
    key = str(os.environ.get("YOUTUBE_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("YOUTUBE_API_KEY is missing")
    query = urllib.parse.urlencode({**params, "key": key})
    return _json_request(f"https://www.googleapis.com/youtube/v3/{resource}?{query}")


def fetch_channel_snapshot() -> dict[str, Any]:
    channel_id = str(YOUTUBE_CHANNEL_ID or "").strip()
    if not channel_id:
        raise RuntimeError("YOUTUBE_CHANNEL_ID is missing")
    channel_payload = _youtube_api(
        "channels",
        {
            "part": "snippet,statistics,contentDetails",
            "id": channel_id,
            "maxResults": "1",
        },
    )
    channels = channel_payload.get("items")
    if not isinstance(channels, list) or not channels or not isinstance(channels[0], dict):
        raise RuntimeError("YouTube channel not found")
    channel = channels[0]
    stats = channel.get("statistics") if isinstance(channel.get("statistics"), dict) else {}
    content = channel.get("contentDetails") if isinstance(channel.get("contentDetails"), dict) else {}
    related = content.get("relatedPlaylists") if isinstance(content.get("relatedPlaylists"), dict) else {}
    uploads = str(related.get("uploads") or "").strip()
    if not uploads:
        raise RuntimeError("YouTube uploads playlist unavailable")

    playlist_payload = _youtube_api(
        "playlistItems",
        {"part": "contentDetails", "playlistId": uploads, "maxResults": "25"},
    )
    ids = [
        str((item.get("contentDetails") or {}).get("videoId") or "").strip()
        for item in (playlist_payload.get("items") or [])
        if isinstance(item, dict)
    ]
    ids = [item for item in ids if item]
    videos: list[dict[str, Any]] = []
    if ids:
        videos_payload = _youtube_api(
            "videos",
            {
                "part": "snippet,statistics,contentDetails",
                "id": ",".join(ids),
                "maxResults": "50",
            },
        )
        for item in videos_payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
            video_stats = item.get("statistics") if isinstance(item.get("statistics"), dict) else {}
            details = item.get("contentDetails") if isinstance(item.get("contentDetails"), dict) else {}
            videos.append(
                {
                    "video_id": str(item.get("id") or ""),
                    "title": str(snippet.get("title") or "")[:180],
                    "published_at": str(snippet.get("publishedAt") or ""),
                    "duration_seconds": _parse_duration_seconds(str(details.get("duration") or "")),
                    "views": int(video_stats.get("viewCount") or 0),
                    "likes": int(video_stats.get("likeCount") or 0),
                    "comments": int(video_stats.get("commentCount") or 0),
                }
            )
    videos.sort(key=lambda item: str(item.get("published_at") or ""), reverse=True)
    last_short, last_long = _latest_by_clean_v2_format(videos)
    return {
        "captured_at": utc_now(),
        "channel_id": channel_id,
        "subscribers": int(stats.get("subscriberCount") or 0),
        "hidden_subscribers": bool(stats.get("hiddenSubscriberCount")),
        "total_views": int(stats.get("viewCount") or 0),
        "video_count": int(stats.get("videoCount") or 0),
        "last_long": last_long,
        "last_short": last_short,
    }


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def append_youtube_snapshot(state: dict[str, Any], snapshot: dict[str, Any]) -> None:
    rows = state.setdefault("youtube_snapshots", [])
    if not isinstance(rows, list):
        raise RuntimeError("malformed state field: youtube_snapshots")
    rows.append(snapshot)
    cutoff = datetime.now(timezone.utc) - timedelta(days=45)
    kept = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        try:
            when = _parse_utc(str(item.get("captured_at") or ""))
        except (TypeError, ValueError):
            continue
        if when >= cutoff:
            kept.append(item)
    kept.sort(key=lambda item: str(item.get("captured_at") or ""))
    state["youtube_snapshots"] = kept[-120:]


def _baseline_snapshot(
    snapshots: list[dict[str, Any]],
    cutoff: datetime,
) -> dict[str, Any] | None:
    eligible: list[tuple[datetime, dict[str, Any]]] = []
    for item in snapshots:
        if not isinstance(item, dict):
            continue
        try:
            when = _parse_utc(str(item.get("captured_at") or ""))
        except (TypeError, ValueError):
            continue
        if when <= cutoff:
            eligible.append((when, item))
    if not eligible:
        return None
    eligible.sort(key=lambda pair: pair[0], reverse=True)
    return eligible[0][1]


def _midnight_baseline_snapshot(
    snapshots: list[dict[str, Any]],
    midnight_utc: datetime,
    *,
    tolerance: timedelta = timedelta(minutes=15),
) -> dict[str, Any] | None:
    nearby: list[tuple[float, dict[str, Any]]] = []
    for item in snapshots:
        if not isinstance(item, dict):
            continue
        try:
            when = _parse_utc(str(item.get("captured_at") or ""))
        except (TypeError, ValueError):
            continue
        distance = abs((when - midnight_utc).total_seconds())
        if distance <= tolerance.total_seconds():
            nearby.append((distance, item))
    if nearby:
        nearby.sort(key=lambda pair: pair[0])
        return nearby[0][1]
    return _baseline_snapshot(snapshots, midnight_utc)


def channel_stats(state: dict[str, Any]) -> dict[str, Any]:
    current = fetch_channel_snapshot()
    existing = [
        item
        for item in state.get("youtube_snapshots", [])
        if isinstance(item, dict)
    ]
    now_utc = _parse_utc(str(current["captured_at"]))
    oman_now = now_utc + OMAN_OFFSET
    oman_midnight = oman_now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_cutoff = oman_midnight - OMAN_OFFSET
    week_cutoff = now_utc - timedelta(days=7)
    today_base = _midnight_baseline_snapshot(existing, today_cutoff)
    week_base = _baseline_snapshot(existing, week_cutoff)

    def delta(base: dict[str, Any] | None, key: str) -> int | None:
        if base is None:
            return None
        return int(current.get(key) or 0) - int(base.get(key) or 0)

    result = {
        **current,
        "views_today": delta(today_base, "total_views"),
        "views_7d": delta(week_base, "total_views"),
        "subscribers_today": delta(today_base, "subscribers"),
        "subscribers_7d": delta(week_base, "subscribers"),
        "today_baseline_at": None if today_base is None else today_base.get("captured_at"),
        "week_baseline_at": None if week_base is None else week_base.get("captured_at"),
    }
    append_youtube_snapshot(state, current)
    return result


def _format_number(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{int(value):,}"


def _video_stats_line(label: str, item: dict[str, Any] | None) -> list[str]:
    if not isinstance(item, dict):
        return [f"{label}: لا يوجد فيديو حديث مناسب"]
    return [
        f"{label}: {str(item.get('title') or 'بدون عنوان')}",
        (
            f"   👁️ {_format_number(int(item.get('views') or 0))} · "
            f"👍 {_format_number(int(item.get('likes') or 0))} · "
            f"💬 {_format_number(int(item.get('comments') or 0))}"
        ),
        f"   https://youtu.be/{str(item.get('video_id') or '')}",
    ]


def render_channel_stats(stats: dict[str, Any]) -> str:
    subscribers = "مخفية" if stats.get("hidden_subscribers") else _format_number(int(stats.get("subscribers") or 0))
    today_views = stats.get("views_today")
    week_views = stats.get("views_7d")
    lines = [
        "📊 إحصائيات قناة نداء اليقظة",
        "",
        f"👥 المشتركون: {subscribers}",
        f"👁️ إجمالي مشاهدات القناة: {_format_number(int(stats.get('total_views') or 0))}",
        f"🎞️ إجمالي الفيديوهات: {_format_number(int(stats.get('video_count') or 0))}",
        "",
        f"📈 مشاهدات اليوم: {'+' + _format_number(today_views) if isinstance(today_views, int) else 'بانتظار أول قياس يومي'}",
        f"📅 مشاهدات آخر 7 أيام: {'+' + _format_number(week_views) if isinstance(week_views, int) else 'بانتظار اكتمال 7 أيام من القياسات'}",
    ]
    if not stats.get("hidden_subscribers"):
        sub_today = stats.get("subscribers_today")
        sub_week = stats.get("subscribers_7d")
        lines.extend(
            [
                f"👤 تغير المشتركين اليوم: {'+' + _format_number(sub_today) if isinstance(sub_today, int) else '—'}",
                f"👤 تغير المشتركين 7 أيام: {'+' + _format_number(sub_week) if isinstance(sub_week, int) else '—'}",
            ]
        )
    lines.extend(["", *_video_stats_line("🎬 آخر فيديو طويل", stats.get("last_long"))])
    lines.extend(["", *_video_stats_line("⚡ آخر شورت", stats.get("last_short"))])
    lines.extend(
        [
            "",
            "ℹ️ أرقام اليوم و7 أيام تُحسب من قياسات يومية لإجمالي القناة.",
        ]
    )
    return "\n".join(lines)

def fetch_trends() -> list[str]:
    try:
        query = urllib.parse.urlencode({"geo": YOUTUBE_REGION})
        request = urllib.request.Request(
            "https://trends.google.com/trending/rss?" + query,
            headers={"User-Agent": "Isco-Clean-V2-Telegram/1"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            root = ET.fromstring(response.read())
        return [
            (item.findtext("title") or "").strip()
            for item in root.findall("./channel/item")
            if (item.findtext("title") or "").strip()
        ][:20]
    except Exception:
        return []


def youtube_search(query: str, *, max_results: int = 6) -> list[dict[str, Any]]:
    key = str(os.environ.get("YOUTUBE_API_KEY") or "").strip()
    if not key:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    params = urllib.parse.urlencode(
        {
            "part": "snippet",
            "q": query[:180],
            "type": "video",
            "order": "viewCount",
            "regionCode": YOUTUBE_REGION,
            "relevanceLanguage": YOUTUBE_LANGUAGE,
            "publishedAfter": cutoff,
            "maxResults": min(10, max_results),
            "key": key,
        }
    )
    first = _json_request("https://www.googleapis.com/youtube/v3/search?" + params)
    ids = [
        str((item.get("id") or {}).get("videoId") or "")
        for item in first.get("items", [])
        if isinstance(item, dict)
    ]
    ids = [item for item in ids if item]
    if not ids:
        return []
    second_params = urllib.parse.urlencode(
        {"part": "snippet,statistics", "id": ",".join(ids), "key": key}
    )
    second = _json_request("https://www.googleapis.com/youtube/v3/videos?" + second_params)
    return [item for item in second.get("items", []) if isinstance(item, dict)]


def market_evidence(query: str) -> tuple[float, dict[str, Any]]:
    try:
        videos = youtube_search(query)
    except Exception:
        videos = []
    now = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    velocities: list[float] = []
    channels: set[str] = set()
    for item in videos:
        snippet = item.get("snippet") or {}
        stats = item.get("statistics") or {}
        published = str(snippet.get("publishedAt") or "")
        try:
            dt = datetime.fromisoformat(published.replace("Z", "+00:00")).astimezone(timezone.utc)
            views = int(stats.get("viewCount") or 0)
        except (ValueError, TypeError):
            continue
        if views <= 0:
            continue
        age = max(1.0, (now - dt).total_seconds() / 86400.0)
        velocity = views / age
        velocities.append(velocity)
        channel = str(snippet.get("channelTitle") or "").strip()
        if channel:
            channels.add(channel)
        rows.append(
            {
                "video_id": str(item.get("id") or ""),
                "title": str(snippet.get("title") or "")[:180],
                "channel": channel[:100],
                "published_at": published,
                "views": views,
                "views_per_day": round(velocity, 1),
            }
        )
    rows.sort(key=lambda x: float(x["views_per_day"]), reverse=True)
    if not velocities:
        return 0.0, {"sample_count": 0, "distinct_channels": 0, "top_samples": []}
    best = min(1.0, math.log10(max(velocities) + 1.0) / 5.0)
    med = min(1.0, math.log10(median(velocities) + 1.0) / 5.0)
    breadth = min(1.0, len(channels) / 4.0)
    score = round(0.45 * best + 0.35 * med + 0.20 * breadth, 3)
    return score, {
        "sample_count": len(rows),
        "distinct_channels": len(channels),
        "max_views_per_day": round(max(velocities), 1),
        "median_views_per_day": round(median(velocities), 1),
        "top_samples": rows[:3],
    }


def _gemini_candidates(trends: list[str], scope: str) -> list[dict[str, str]]:
    key = str(os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key:
        return []
    trend_text = "\n".join(f"- {item}" for item in trends[:12]) or "- لا توجد إشارات Trends موثوقة"
    scope_instruction = (
        "الأفكار يجب أن تصلح لشورت واحد مكثف بفكرة واحدة مكتملة."
        if scope == "short"
        else "الأفكار يجب أن تتحمل حلقة طويلة ذات عمق وبناء واضح."
    )
    prompt = f"""أنت محرر أبحاث لقناة عربية اسمها نداء اليقظة عن التطور الشخصي والوعي النفسي بأسلوب متفائل وواقعي.
{scope_instruction}
اقترح 8 أفكار أصلية مناسبة للنطاق المطلوب. تجنب التشخيص الطبي والوعود المبالغ فيها والتكرار.
استخدم إشارات Google Trends التالية كخلفية فقط إذا كانت ذات صلة، ولا تجبرها على المجال:
{trend_text}
لكل فكرة أعد title وmarket_query وreason. market_query عبارة بحث عربية محايدة من 2-7 كلمات.
أعد JSON فقط بالشكل:
{{"candidates":[{{"title":"...","market_query":"...","reason":"..."}}]}}"""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.7},
    }
    try:
        data = _json_request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(MODEL, safe='')}:generateContent?key={urllib.parse.quote(key)}",
            method="POST",
            payload=payload,
            headers={"Content-Type": "application/json"},
            timeout=35,
        )
        parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
        text = "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))
        parsed = json.loads(text)
        rows = parsed.get("candidates") if isinstance(parsed, dict) else None
        if not isinstance(rows, list):
            return []
        result = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = " ".join(str(row.get("title") or "").split())[:180]
            query = " ".join(str(row.get("market_query") or "").split())[:120]
            reason = " ".join(str(row.get("reason") or "").split())[:240]
            if title and query:
                result.append({"title": title, "market_query": query, "reason": reason})
        return result[:8]
    except Exception as exc:
        print(f"Gemini research fallback activated: {type(exc).__name__}")
        return []


def _candidate_pool(scope: str) -> list[dict[str, str]]:
    rows = _gemini_candidates(fetch_trends(), scope)
    for title, query in FALLBACK_IDEAS:
        if not any(same_topic(title, item.get("title", "")) for item in rows):
            rows.append(
                {
                    "title": title,
                    "market_query": query,
                    "reason": "حاجة عملية مستمرة تناسب هوية القناة ويمكن قياس اهتمام YouTube الحديث بها.",
                }
            )
    return rows[:12]


def _research_pack(evidence: dict[str, Any]) -> list[dict[str, str]]:
    pack = []
    for sample in evidence.get("top_samples", [])[:3]:
        if not isinstance(sample, dict):
            continue
        video_id = str(sample.get("video_id") or "").strip()
        title = str(sample.get("title") or "").strip()
        if not video_id or not title:
            continue
        pack.append(
            {
                "source_title": title,
                "source_url": f"https://youtu.be/{video_id}",
                "claim_scope": (
                    "دليل سوقي على وجود محتوى واهتمام حديث حول الموضوع فقط؛ "
                    "لا يثبت تشخيصًا نفسيًا أو سببية أو نسبة أو ادعاءً علميًا."
                ),
            }
        )
    return pack


def research(state: dict[str, Any], scope: str) -> dict[str, Any]:
    if scope not in SCOPES:
        raise RuntimeError("unsupported scope")
    historical = [
        str(item.get("title") or "")
        for item in state.get("ideas", [])
        if isinstance(item, dict)
    ]
    measured: list[dict[str, Any]] = []
    for raw in _candidate_pool(scope):
        title = str(raw.get("title") or "").strip()
        if (
            not title
            or any(same_topic(title, old) for old in historical)
            or any(same_topic(title, str(item.get("title") or "")) for item in measured)
        ):
            continue
        score, evidence = market_evidence(str(raw.get("market_query") or title))
        reason = str(raw.get("reason") or "").strip()
        if evidence.get("sample_count", 0):
            reason = reason.strip()
        measured.append(
            {
                "idea_id": "idea-" + secrets.token_hex(5),
                "title": title,
                "normalized_title": normalize_title(title),
                "market_query": str(raw.get("market_query") or title)[:120],
                "reason": reason[:320],
                "market_score": score,
                "market_evidence": evidence,
                "research_pack": [],
                "selected": False,
                "created_at": utc_now(),
            }
        )
        if len(measured) >= 8:
            break
    measured.sort(
        key=lambda row: (
            int((row.get("market_evidence") or {}).get("distinct_channels", 0)),
            int((row.get("market_evidence") or {}).get("sample_count", 0)),
            float(row.get("market_score") or 0.0),
        ),
        reverse=True,
    )
    evidence_backed = [
        item
        for item in measured
        if int((item.get("market_evidence") or {}).get("sample_count", 0)) >= 1
    ]
    chosen = evidence_backed[:3]
    state["ideas"].extend(measured)
    if not chosen:
        raise RuntimeError("research found no unused evidence-backed candidates")
    session_id = secrets.token_hex(4)
    state["sessions"][session_id] = {
        "session_id": session_id,
        "scope": scope,
        "idea_ids": [item["idea_id"] for item in chosen],
        "created_at": utc_now(),
    }
    # keep state bounded
    sessions = sorted(
        state["sessions"].values(),
        key=lambda item: str(item.get("created_at") or ""),
        reverse=True,
    )[:40]
    state["sessions"] = {item["session_id"]: item for item in sessions}
    return {"session_id": session_id, "scope": scope, "candidates": chosen}


def _idea_by_id(state: dict[str, Any], idea_id: str) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in state.get("ideas", [])
            if isinstance(item, dict) and item.get("idea_id") == idea_id
        ),
        None,
    )


def _request_hash(request: dict[str, Any]) -> str:
    # Bind only immutable editorial authority. Runtime lifecycle fields such as
    # status/confirmed_at/dispatched_at must never change the approved identity.
    immutable_keys = (
        "schema_version",
        "request_id",
        "source",
        "scope",
        "approved_by_user",
        "approved_topic",
        "research_pack",
        "idea_id",
        "selected_at",
    )
    payload = {key: request.get(key) for key in immutable_keys}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def select_candidate(state: dict[str, Any], session_id: str, index: int) -> dict[str, Any]:
    session = state.get("sessions", {}).get(session_id)
    if not isinstance(session, dict):
        raise RuntimeError("research session expired")
    if session.get("closed_at") or session.get("obsolete_at"):
        raise RuntimeError("research session is closed")
    ids = session.get("idea_ids")
    if not isinstance(ids, list) or not 0 <= index < len(ids):
        raise RuntimeError("invalid candidate selection")
    idea = _idea_by_id(state, str(ids[index]))
    if not isinstance(idea, dict):
        raise RuntimeError("candidate missing from idea archive")
    pack = _research_pack(idea.get("market_evidence") or {})
    if not pack:
        raise RuntimeError("selected candidate has no usable research evidence")
    idea["research_pack"] = pack
    idea["selected"] = True
    idea["selected_at"] = utc_now()
    request_id = "req-" + secrets.token_hex(6)
    request: dict[str, Any] = {
        "schema_version": 1,
        "request_id": request_id,
        "source": "clean_v2_telegram_editorial_lite",
        "scope": str(session["scope"]),
        "approved_by_user": True,
        "approved_topic": str(idea["title"]),
        "research_pack": list(pack),
        "idea_id": str(idea["idea_id"]),
        "selected_at": utc_now(),
        "status": "awaiting_confirmation",
        "confirmed_at": None,
        "dispatched_at": None,
    }
    request["request_sha256"] = _request_hash(request)
    state["requests"][request_id] = request
    state["current_request_id"] = request_id
    session["closed_at"] = utc_now()
    session["selected_index"] = index
    return request


def cancel_current(state: dict[str, Any]) -> dict[str, Any]:
    request_id = str(state.get("current_request_id") or "")
    request = state.get("requests", {}).get(request_id)
    if not isinstance(request, dict):
        raise RuntimeError("no selected request is waiting for cancellation")
    if request.get("request_sha256") != _request_hash(request):
        raise RuntimeError("selected request integrity check failed")
    if request.get("status") != "awaiting_confirmation":
        raise RuntimeError("selected request can no longer be cancelled")
    request["status"] = "cancelled"
    request["cancelled_at"] = utc_now()
    state["current_request_id"] = None
    return request


def confirm_current(state: dict[str, Any]) -> dict[str, Any]:
    request_id = str(state.get("current_request_id") or "")
    request = state.get("requests", {}).get(request_id)
    if not isinstance(request, dict):
        raise RuntimeError("no selected request is waiting for confirmation")
    if request.get("request_sha256") != _request_hash(request):
        raise RuntimeError("selected request integrity check failed")
    status = str(request.get("status") or "")
    if status == "dispatched":
        return {"already_dispatched": True, "request": request}
    if status not in {"awaiting_confirmation", "confirmed_pending_dispatch"}:
        raise RuntimeError("selected request is not confirmable")
    if status == "awaiting_confirmation":
        request["status"] = "confirmed_pending_dispatch"
        request["confirmed_at"] = utc_now()
        request["request_sha256"] = _request_hash(request)
    return {"already_dispatched": False, "request": request}


def mark_dispatched(state: dict[str, Any], request_id: str, request_sha256: str) -> dict[str, Any]:
    request = state.get("requests", {}).get(request_id)
    if not isinstance(request, dict):
        raise RuntimeError("dispatch request is missing")
    if request.get("request_sha256") != request_sha256 or request_sha256 != _request_hash(request):
        raise RuntimeError("dispatch request hash mismatch")
    if request.get("status") not in {"confirmed_pending_dispatch", "dispatched"}:
        raise RuntimeError("dispatch request is not confirmed")
    request["status"] = "dispatched"
    request["dispatched_at"] = request.get("dispatched_at") or utc_now()
    request["request_sha256"] = _request_hash(request)
    return request


def scope_keyboard() -> list[list[dict[str, str]]]:
    return [
        [{"text": "🎬 Long فقط", "callback_data": "scope:long"}],
        [{"text": "🎬➕⚡ Long + Short", "callback_data": "scope:bundle"}],
        [{"text": "⚡ Short فقط", "callback_data": "scope:short"}],
    ]


def render_candidates(result: dict[str, Any]) -> tuple[str, list[list[dict[str, str]]]]:
    count = len(result["candidates"])
    noun = "فكرة" if count == 1 else "فكرتان" if count == 2 else "أفكار"
    lines = [f"🔎 {count} {noun} مناسبة", ""]
    rows = []
    for index, item in enumerate(result["candidates"], 1):
        evidence = item.get("market_evidence") or {}
        lines.extend(
            [
                f"{index}) {item['title']}",
                f"   {item['reason']}",
                (
                    f"   اهتمام حديث: وجدنا {int(evidence.get('sample_count', 0))} فيديوهات "
                    f"حول الفكرة من {int(evidence.get('distinct_channels', 0))} قنوات مختلفة "
                    f"خلال آخر {WINDOW_DAYS} يومًا."
                ),
                "",
            ]
        )
        rows.append(
            [
                {
                    "text": f"✅ اختيار {index}",
                    "callback_data": f"pick:{result['session_id']}:{index - 1}",
                }
            ]
        )
    lines.append("الاختيار لا يبدأ الإنتاج. بعد الاختيار يلزم إرسال «تأكيد الإنتاج» حرفيًا.")
    return "\n".join(lines), rows


def render_selection_confirmation(request: dict[str, Any]) -> str:
    scope_label = {"long": "فيديو طويل فقط", "bundle": "فيديو طويل + شورت", "short": "شورت فقط"}[str(request["scope"])]
    pack = [item for item in request.get("research_pack", []) if isinstance(item, dict)]
    lines = [
        "✅ تم اختيار الفكرة وحفظ مصادر البحث",
        "",
        f"الموضوع: {request['approved_topic']}",
        f"النطاق: {scope_label}",
    ]
    if pack:
        lines.extend(["", "🔎 أهم المصادر قبل التأكيد:"])
        for index, source in enumerate(pack[:2], 1):
            lines.append(f"{index}) {str(source.get('source_title') or 'مصدر')}")
            url = str(source.get("source_url") or "").strip()
            if url:
                lines.append(f"   {url}")
    lines.extend(
        [
            "",
            "لم يبدأ الإنتاج بعد.",
            f"إذا كان القرار نهائيًا أرسل حرفيًا:\n{CONFIRM_TEXT}",
        ]
    )
    return "\n".join(lines)


def _actor_chat(update: dict[str, Any]) -> tuple[str, str]:
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        actor = callback.get("from") or {}
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        return str(actor.get("id") or ""), str(chat.get("id") or "")
    message = update.get("message") or {}
    actor = message.get("from") or {}
    chat = message.get("chat") or {}
    return str(actor.get("id") or ""), str(chat.get("id") or "")


def load_runtime_status() -> dict[str, Any]:
    raw_path = str(os.environ.get("TELEGRAM_RUNTIME_STATE_PATH") or "").strip()
    if not raw_path:
        return {}
    try:
        value = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def render_production_status(runtime: dict[str, Any]) -> str:
    if not runtime.get("active"):
        return "⚪ لا يوجد إنتاج يعمل الآن."
    scope_label = {
        "long": "🎬 فيديو طويل",
        "short": "⚡ شورت",
        "bundle": "🎬 طويل + ⚡ شورت",
    }.get(str(runtime.get("scope") or ""), "إنتاج")
    kind = str(runtime.get("kind") or "")
    if str(runtime.get("scope") or "") == "bundle" and kind:
        scope_label += " — " + ("الطويل" if kind == "long" else "الشورت")
    lines = [
        "🟢 يوجد إنتاج يعمل الآن",
        f"النوع: {scope_label}",
        f"آخر مرحلة: {str(runtime.get('stage') or 'بدأ التشغيل')}",
    ]
    topic = str(runtime.get("topic") or "").strip()
    if topic:
        lines.append(f"الموضوع: {topic}")
    run_url = str(runtime.get("run_url") or "").strip()
    if run_url:
        lines.extend(["", f"متابعة التشغيل: {run_url}"])
    return "\n".join(lines)


def render_last_success(runtime: dict[str, Any]) -> tuple[str, list[list[dict[str, str]]] | None]:
    last = runtime.get("last_success")
    if not isinstance(last, dict):
        return "⚪ لا يوجد إنتاج ناجح محفوظ بعد.", None
    scope_label = {
        "long": "🎬 فيديو طويل",
        "short": "⚡ شورت",
        "bundle": "🎬 طويل + ⚡ شورت",
    }.get(str(last.get("scope") or ""), "إنتاج")
    topic = str(last.get("topic") or "").strip()
    url = str(last.get("artifact_url") or "").strip()
    lines = ["✅ آخر إنتاج ناجح", f"النوع: {scope_label}"]
    if topic:
        lines.append(f"الموضوع: {topic}")
    keyboard = None
    if url:
        keyboard = [[{"text": "🎥 فتح الفيديو النهائي", "url": url}]]
    return "\n".join(lines), keyboard


def authorized(update: dict[str, Any]) -> bool:
    expected = str(os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    actor, chat = _actor_chat(update)
    return bool(expected and actor == expected and chat == expected)


def handle_update(state: dict[str, Any], update: dict[str, Any], dispatch_path: Path) -> None:
    if not authorized(update):
        raise RuntimeError("unauthorized Telegram update")
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        data = str(callback.get("data") or "")
        if data.startswith("scope:"):
            scope = data.split(":", 1)[1]
            try:
                result = research(state, scope)
            except Exception as exc:
                print(f"Telegram research failed: {type(exc).__name__}")
                send_telegram(
                    "⚠️ لم يُعثر على مواضيع مناسبة بهذه المعايير الآن. "
                    "لم يبدأ أي إنتاج؛ جرّب معايير مختلفة أو أعد البحث لاحقًا."
                )
                return
            text, keyboard = render_candidates(result)
            send_telegram(text, keyboard)
            return
        if data.startswith("pick:"):
            parts = data.split(":")
            try:
                if len(parts) != 3:
                    raise RuntimeError("malformed pick callback")
                request = select_candidate(state, parts[1], int(parts[2]))
            except Exception:
                send_telegram("⚠️ هذا الاختيار لم يعد صالحًا. اطلب /research من جديد.")
                return
            send_telegram(render_selection_confirmation(request))
            return
        raise RuntimeError("unsupported callback")

    message = update.get("message") or {}
    text = str(message.get("text") or "").strip()
    if text in {"/start", "start", "ابدأ", "ابدأ البوت"}:
        send_telegram(
            "👋 مرحبًا بك في مساعد نداء اليقظة\n\n"
            "1) ابحث عن فكرة مناسبة للقناة.\n"
            "2) اختر الفكرة التي تناسبك.\n"
            "3) أرسل «تأكيد الإنتاج» فقط عندما تريد بدء الإنتاج فعليًا.\n\n"
            "الاختيار وحده لا يبدأ أي إنتاج.\n"
            "📊 للإحصائيات استخدم /stats.\n"
            "🔎 للبحث استخدم /research.",
            scope_keyboard(),
        )
        return
    if text in {"/menu", "menu", "/research", "research", "بحث"}:
        send_telegram(
            "🔎 اختر نوع المحتوى الذي تريد البحث له. لن يبدأ الإنتاج قبل تأكيدك النهائي.",
            scope_keyboard(),
        )
        return
    if text in {"/status", "status", "الحالة", "حالة الإنتاج", "حاله الانتاج"}:
        send_telegram(render_production_status(load_runtime_status()))
        return
    if text in {"/last", "last", "آخر إنتاج", "اخر انتاج"}:
        last_text, last_keyboard = render_last_success(load_runtime_status())
        send_telegram(last_text, last_keyboard)
        return
    if text in {"/stats", "stats", "إحصائيات", "الاحصائيات", "الإحصائيات"}:
        try:
            stats = channel_stats(state)
        except Exception as exc:
            print(f"YouTube stats failed: {type(exc).__name__}")
            send_telegram("⚠️ تعذر تحديث إحصائيات YouTube الآن. لم يتأثر البحث أو الإنتاج.")
            return
        send_telegram(render_channel_stats(stats))
        return
    if text in {"/cancel", "cancel", "إلغاء", "الغاء"}:
        try:
            request = cancel_current(state)
        except Exception:
            send_telegram("⚠️ لا يوجد اختيار معلّق يمكن إلغاؤه الآن.")
            return
        send_telegram(f"🛑 تم إلغاء الاختيار المعلّق:\n{request['approved_topic']}\n\nلم يبدأ أي إنتاج.")
        return
    if text == CONFIRM_TEXT:
        try:
            result = confirm_current(state)
        except Exception:
            send_telegram("⚠️ لا يوجد اختيار صالح ينتظر التأكيد. اطلب /research واختر فكرة أولًا.")
            return
        request = result["request"]
        if result["already_dispatched"]:
            send_telegram("✅ هذا الطلب أُرسل للإنتاج بالفعل. لن أنشئ محاولة مكررة.")
            return
        dispatch_path.parent.mkdir(parents=True, exist_ok=True)
        dispatch_path.write_text(
            json.dumps(
                {
                    "request_id": request["request_id"],
                    "request_sha256": request["request_sha256"],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        send_telegram(
            "🚀 تم تأكيد الإنتاج. حُفظ القرار أولًا، وسيُرسل الآن إلى Clean V2.\n"
            f"الموضوع: {request['approved_topic']}"
        )
        return
    send_telegram("استخدم /research لطلب 3 أفكار جديدة، /stats لإحصائيات القناة، أو اختر فكرة ثم أرسل «تأكيد الإنتاج» حرفيًا.")


def materialize_brief(state: dict[str, Any], request_id: str, request_sha256: str, fmt: str, output: Path) -> dict[str, Any]:
    request = state.get("requests", {}).get(request_id)
    if not isinstance(request, dict):
        raise RuntimeError("production request not found")
    if request.get("status") != "dispatched":
        raise RuntimeError("production request has not crossed the dispatch gate")
    if request.get("request_sha256") != request_sha256 or request_sha256 != _request_hash(request):
        raise RuntimeError("production request integrity mismatch")
    allowed = FORMATS_BY_SCOPE.get(str(request.get("scope") or ""), [])
    if fmt not in allowed:
        raise RuntimeError("requested format is outside approved scope")
    brief = {
        "approved_by_user": True,
        "approved_topic": str(request["approved_topic"]),
        "format": fmt,
        "language": "ar",
        "audience": "Arabic-speaking adults",
        "editorial_intent": (
            "محتوى عربي فصيح طبيعي، متفائل وواقعي، واضح ومفيد، "
            "مع تجنب المبالغة والادعاءات غير المدعومة."
        ),
        "research_pack": list(request.get("research_pack") or []),
        "hard_constraints": [
            "No fabricated facts.",
            "Use research_pack only within each source claim_scope.",
            "One natural Arabic narrator only.",
            *(["Complete Short must not exceed 30 seconds."] if fmt == "short" else []),
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(brief, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return brief


def _decode_update(value: str) -> dict[str, Any]:
    raw = base64.b64decode(value.encode("ascii"), validate=True)
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise RuntimeError("Telegram update must be an object")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    handle = sub.add_parser("handle")
    handle.add_argument("--state", type=Path, required=True)
    handle.add_argument("--update-b64", required=True)
    handle.add_argument("--dispatch", type=Path, required=True)

    mark = sub.add_parser("mark-dispatched")
    mark.add_argument("--state", type=Path, required=True)
    mark.add_argument("--request-id", required=True)
    mark.add_argument("--request-sha256", required=True)

    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--state", type=Path, required=True)

    brief = sub.add_parser("materialize-brief")
    brief.add_argument("--state", type=Path, required=True)
    brief.add_argument("--request-id", required=True)
    brief.add_argument("--request-sha256", required=True)
    brief.add_argument("--format", choices=("film", "short"), required=True)
    brief.add_argument("--output", type=Path, required=True)

    args = parser.parse_args()
    state = load_state(args.state)

    if args.command == "handle":
        args.dispatch.unlink(missing_ok=True)
        handle_update(state, _decode_update(args.update_b64), args.dispatch)
        save_state(args.state, state)
        return 0
    if args.command == "mark-dispatched":
        request = mark_dispatched(state, args.request_id, args.request_sha256)
        save_state(args.state, state)
        print(json.dumps({"request_id": request["request_id"], "status": request["status"]}))
        return 0
    if args.command == "snapshot":
        snapshot_value = fetch_channel_snapshot()
        append_youtube_snapshot(state, snapshot_value)
        save_state(args.state, state)
        print(json.dumps(snapshot_value, ensure_ascii=False, sort_keys=True))
        return 0
    materialize_brief(state, args.request_id, args.request_sha256, args.format, args.output)
    print(json.dumps({"request_id": args.request_id, "format": args.format, "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
