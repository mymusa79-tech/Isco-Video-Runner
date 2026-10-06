from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

try:
    from scripts.research_relevance_filter import market_sample_relevance
    from scripts import telegram_resume_history as resume_history
except ModuleNotFoundError:
    from research_relevance_filter import market_sample_relevance
    import telegram_resume_history as resume_history

STATE_VERSION = 1
CONFIRM_TEXT = "تأكيد الإنتاج"
SCOPES = {"long", "bundle", "short", "podcast"}
FORMATS_BY_SCOPE = {
    "long": ["film"],
    "bundle": ["film"],
    "short": ["short"],
    "podcast": ["podcast"],
}
MODEL = os.environ.get("GEMINI_CONTENT_MODEL", "gemini-3.7-flash")
YOUTUBE_REGION = os.environ.get("YOUTUBE_REGION", "SA")
YOUTUBE_LANGUAGE = os.environ.get("YOUTUBE_LANGUAGE", "ar")
WINDOW_DAYS = 30
CLEAN_V2_SHORT_SAFETY_MAX_SECONDS = 120
LONGFORM_MIN_PUBLISH_SPACING_DAYS_ENV = "CLEAN_V2_LONGFORM_MIN_PUBLISH_SPACING_DAYS"
DEFAULT_LONGFORM_MIN_PUBLISH_SPACING_DAYS = 10.0
YOUTUBE_CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID", "UC_fmWGRen6QUQNd4Dj80MgA")
OMAN_OFFSET = timedelta(hours=4)
CLEAN_V2_DELIVERY_TAG_PREFIX = "clean-v2-final-"
LIBRARY_ORDER = ("long", "short", "podcast")
LIBRARY_LABELS = {
    "long": ("🎬", "طويل"),
    "short": ("⚡", "شورت"),
    "podcast": ("🎙️", "بودكاست"),
}

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

PODCAST_FALLBACK_IDEAS = [
    ("لماذا نعود إلى عادة نعرف أنها تؤذينا رغم وضوح قرارنا بالتوقف؟", "العودة للعادات بعد قرار التوقف"),
    ("لماذا يتحول السعي إلى تحسين حياتنا أحيانًا إلى شعور دائم بأننا غير كافين؟", "تطوير الذات والشعور بعدم الكفاية"),
    ("ماذا يحدث عندما نبني يومنا كله على انتظار الدافع؟", "انتظار الدافع وتأثيره على السلوك"),
    ("لماذا يبدو البدء من جديد مريحًا أكثر من إكمال ما بدأناه؟", "إدمان البدايات وترك المشاريع"),
    ("كيف تتحول المقارنة من ملاحظة عابرة إلى مقياس نحاكم به حياتنا؟", "المقارنة الاجتماعية وتقييم الذات"),
    ("لماذا لا تحل إدارة الوقت مشكلة يوم لا نعرف فيه ما يستحق وقتنا أصلًا؟", "إدارة الوقت وتحديد الأولويات"),
    ("متى تكون الراحة استعادة للطاقة، ومتى تصبح طريقة مؤجلة لتجنب ما نخافه؟", "الراحة وتجنب المسؤوليات"),
    ("لماذا نعرف النصيحة الصحيحة ولا يتغير سلوكنا رغم ذلك؟", "الفجوة بين المعرفة والسلوك"),
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
        "provider_cooldown_until": None,
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


def _is_podcast_upload(item: dict[str, Any]) -> bool:
    return "خارج النص" in str(item.get("title") or "")


def _latest_by_clean_v2_format(
    videos: list[dict[str, Any]],
    *,
    longform_kind: str = "any",
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    last_short = next(
        (
            item
            for item in videos
            if 0 < int(item.get("duration_seconds") or 0) <= CLEAN_V2_SHORT_SAFETY_MAX_SECONDS
        ),
        None,
    )
    longform = [
        item
        for item in videos
        if int(item.get("duration_seconds") or 0) > CLEAN_V2_SHORT_SAFETY_MAX_SECONDS
    ]
    if longform_kind == "podcast":
        longform = [item for item in longform if _is_podcast_upload(item)]
    elif longform_kind == "long":
        longform = [item for item in longform if not _is_podcast_upload(item)]
    elif longform_kind != "any":
        raise ValueError("unsupported longform_kind")
    last_long = longform[0] if longform else None
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
    last_short, last_long = _latest_by_clean_v2_format(videos, longform_kind="long")
    _, last_podcast = _latest_by_clean_v2_format(videos, longform_kind="podcast")
    return {
        "captured_at": utc_now(),
        "channel_id": channel_id,
        "subscribers": int(stats.get("subscriberCount") or 0),
        "hidden_subscribers": bool(stats.get("hiddenSubscriberCount")),
        "total_views": int(stats.get("viewCount") or 0),
        "video_count": int(stats.get("videoCount") or 0),
        "last_long": last_long,
        "last_podcast": last_podcast,
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


# Why a research run found nothing. Only fixed codes are stored (never raw error
# text), so no secret or provider payload can reach the Telegram message.
_RESEARCH_FAILURES: set[str] = set()

_RESEARCH_FAILURE_MESSAGES = {
    "youtube_key_missing": "مفتاح YouTube Data API غير مضبوط.",
    "youtube_quota": (
        "حصة YouTube Data API اليومية انتهت؛ تتجدد عند منتصف الليل بتوقيت المحيط الهادئ "
        "(حوالي 11 صباحًا بتوقيت مسقط)."
    ),
    "youtube_error": "تعذر الوصول إلى YouTube Data API.",
    "gemini_unavailable": "تعذر توليد أفكار جديدة من Gemini، فاستُخدمت الأفكار الاحتياطية فقط.",
}


def _classify_youtube_failure(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError) and exc.code in (403, 429):
        try:
            body = exc.read().decode("utf-8", "replace").lower()
        except Exception:
            body = ""
        if "quota" in body or "ratelimit" in body or exc.code == 429:
            return "youtube_quota"
    return "youtube_error"


def research_failure_reason() -> str:
    """Arabic reason for an empty research result, from the fixed codes recorded."""
    order = ("youtube_key_missing", "youtube_quota", "youtube_error", "gemini_unavailable")
    reasons = [_RESEARCH_FAILURE_MESSAGES[code] for code in order if code in _RESEARCH_FAILURES]
    return " ".join(reasons)


def youtube_search(query: str, *, max_results: int = 6) -> list[dict[str, Any]]:
    key = str(os.environ.get("YOUTUBE_API_KEY") or "").strip()
    if not key:
        _RESEARCH_FAILURES.add("youtube_key_missing")
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
    except Exception as exc:
        _RESEARCH_FAILURES.add(_classify_youtube_failure(exc))
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
        relevant, relevance_overlap = market_sample_relevance(query, snippet)
        if not relevant:
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
                "relevance_overlap": relevance_overlap,
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


def _scope_research_instruction(scope: str) -> str:
    if scope == "short":
        return "الأفكار يجب أن تصلح لشورت واحد مكثف بفكرة واحدة مكتملة."
    if scope == "bundle":
        return (
            "كل فكرة يجب أن تتحمل حلقة طويلة ذات عمق وبناء واضح، "
            "وفي الوقت نفسه تسمح باشتقاق شورت قوي من نفس الحلقة دون إنتاج مستقل أو إعادة كتابة الحلقة كاملة."
        )
    if scope == "podcast":
        return (
            "اختر أفكارًا لبرنامج «خارج النص» تناسب هويته الثابتة كحوار listener-proxy: "
            "لكل فكرة سؤال مركزي حقيقي يستطيع المستمع A أن يقوله بجملة قصيرة وطبيعية، "
            "ويملك صوت القناة B إجابة متدرجة تكشف السبب أو المفارقة أو الطبقة الخفية مباشرة ثم تتعمق دون حشو. "
            "يجب أن يتغير فهم المستمع بوضوح بين البداية والنهاية، وأن تكون الفكرة قابلة للبحث "
            "وليست مجرد موضوع عام أو قائمة نصائح أو تحفيزًا عامًا. تجنب أسلوب المضيف/الضيف، المقابلات المصطنعة، "
            "العناوين من نوع «5 طرق»، والتناوب الآلي بين السؤال والجواب. يجب أن تستحق الفكرة حلقة كاملة وأن تعمل صوتيًا وحدها."
        )
    return "الأفكار يجب أن تتحمل حلقة طويلة ذات عمق وبناء واضح."


def _gemini_candidates(trends: list[str], scope: str) -> list[dict[str, str]]:
    key = str(os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key:
        return []
    trend_text = "\n".join(f"- {item}" for item in trends[:12]) or "- لا توجد إشارات Trends موثوقة"
    scope_instruction = _scope_research_instruction(scope)
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
        _RESEARCH_FAILURES.add("gemini_unavailable")
        return []


def _candidate_pool(scope: str) -> list[dict[str, str]]:
    rows = _gemini_candidates(fetch_trends(), scope)
    fallback_ideas = PODCAST_FALLBACK_IDEAS if scope == "podcast" else FALLBACK_IDEAS
    for title, query in fallback_ideas:
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
    _RESEARCH_FAILURES.clear()
    obsolete_at = utc_now()
    for existing_session in state.get("sessions", {}).values():
        if (
            isinstance(existing_session, dict)
            and not existing_session.get("closed_at")
            and not existing_session.get("obsolete_at")
        ):
            existing_session["obsolete_at"] = obsolete_at
    # Only a topic the user actually selected should be permanently excluded from
    # future research. A topic that was merely shown as a candidate (in particular
    # a small fixed fallback idea, offered whenever Gemini's live generation fails)
    # must remain eligible again later, or the tiny fallback pool exhausts itself
    # after being surfaced once and every future research call fails outright.
    historical = [
        str(item.get("title") or "")
        for item in state.get("ideas", [])
        if isinstance(item, dict) and item.get("selected_at")
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


class ProviderCooldownActive(RuntimeError):
    """Raised when a prior production run just failed pre-layer (provider
    exhaustion) and the cooldown window from
    resume_history.apply_provider_cooldown_if_exhausted() has not elapsed yet."""

    def __init__(self, remaining_seconds: int) -> None:
        self.remaining_seconds = max(0, int(remaining_seconds))
        super().__init__(f"provider cooldown active for {self.remaining_seconds}s")


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


def _stage_confirmation_dispatch(
    state: dict[str, Any],
    dispatch_path: Path,
    *,
    expected_request_id: str | None = None,
) -> dict[str, Any]:
    current_request_id = str(state.get("current_request_id") or "")
    if expected_request_id is not None and expected_request_id != current_request_id:
        raise RuntimeError("confirmation button is stale")
    result = confirm_current(state)
    if result["already_dispatched"]:
        return result
    remaining = resume_history.provider_cooldown_remaining_seconds(state)
    if remaining > 0:
        raise ProviderCooldownActive(remaining)
    request = result["request"]
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
    return result


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


def main_menu_keyboard() -> list[list[dict[str, str]]]:
    return [
        [{"text": "🔎 بحث جديد", "callback_data": "main:research"}],
        [
            {"text": "📚 المحفوظات", "callback_data": "main:saved"},
            {"text": "✅ المستعملة", "callback_data": "main:used"},
        ],
        [
            {"text": "📊 الإحصائيات", "callback_data": "main:stats"},
            {"text": "🟢 حالة الإنتاج", "callback_data": "main:status"},
        ],
        [{"text": "🎥 آخر إنتاج", "callback_data": "main:last"}],
        [{"text": "❌ إلغاء الاختيار", "callback_data": "main:cancel"}],
    ]


def render_main_menu() -> str:
    return (
        "🏠 الرئيسية\n\n"
        "كل الأدوات هنا داخل قائمة واحدة. اختر ما تريد؛ "
        "ولا يبدأ الإنتاج إلا بعد «تأكيد الإنتاج»."
    )


def scope_keyboard() -> list[list[dict[str, str]]]:
    # Film and Podcast already attempt one zero-call derived Short after the
    # certified parent succeeds. Keep legacy "bundle" backend compatibility for
    # old saved/state records, but do not expose a duplicate choice in Telegram.
    return [
        [{"text": "🎬 فيديو طويل", "callback_data": "scope:long"}],
        [{"text": "⚡ شورت", "callback_data": "scope:short"}],
        [{"text": "🎙️ خارج النص", "callback_data": "scope:podcast"}],
        [{"text": "↩️ الرئيسية", "callback_data": "main:home"}],
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
    lines.append(
        "الاختيار لا يبدأ الإنتاج. بعد الاختيار يلزم تأكيد منفصل من الزر الآمن "
        "أو بإرسال «تأكيد الإنتاج» حرفيًا."
    )
    return "\n".join(lines), rows


def _longform_min_publish_spacing_days() -> float:
    raw = str(
        os.environ.get(
            LONGFORM_MIN_PUBLISH_SPACING_DAYS_ENV,
            DEFAULT_LONGFORM_MIN_PUBLISH_SPACING_DAYS,
        )
    ).strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = DEFAULT_LONGFORM_MIN_PUBLISH_SPACING_DAYS
    return max(0.0, value)


def publication_spacing_warning(
    request: dict[str, Any],
    *,
    snapshot: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> str:
    """Return a non-blocking owner warning before Long/Podcast confirmation."""
    scope = str(request.get("scope") or "")
    if scope not in {"long", "bundle", "podcast"}:
        return ""
    minimum_days = _longform_min_publish_spacing_days()
    if minimum_days <= 0:
        return ""

    channel = snapshot if isinstance(snapshot, dict) else fetch_channel_snapshot()
    latest_key = "last_podcast" if scope == "podcast" else "last_long"
    latest = channel.get(latest_key)
    if not isinstance(latest, dict):
        return ""
    published_at = str(latest.get("published_at") or "").strip()
    if not published_at:
        return ""
    try:
        published = _parse_utc(published_at)
    except (TypeError, ValueError):
        return ""

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    elapsed_days = max(0.0, (current - published).total_seconds() / 86400.0)
    if elapsed_days >= minimum_days:
        return ""

    label = "بودكاست «خارج النص»" if scope == "podcast" else "فيديو طويل"
    remaining = max(0.0, minimum_days - elapsed_days)
    return (
        f"⚠️ تنبيه تباعد النشر: آخر {label} نُشر قبل {elapsed_days:.1f} يوم. "
        f"الحد الأدنى المضبوط حاليًا {minimum_days:g} أيام "
        f"(المتبقي نحو {remaining:.1f} يوم). هذا تحذير فقط؛ يمكنك تأكيد الإنتاج إن أردت."
    )


def _safe_publication_spacing_warning(request: dict[str, Any]) -> str:
    try:
        return publication_spacing_warning(request)
    except Exception as exc:
        print(f"YouTube publication spacing check failed: {type(exc).__name__}")
        return ""


def render_selection_confirmation(
    request: dict[str, Any],
    *,
    spacing_warning: str = "",
) -> str:
    scope_label = {
        "long": "فيديو طويل — يحاول استخراج شورت تلقائيًا بعد نجاح الطويل",
        "bundle": "فيديو طويل + شورت (خيار قديم)",
        "short": "شورت مستقل",
        "podcast": "خارج النص — يحاول استخراج شورت تلقائيًا بعد نجاح الحلقة",
    }[str(request["scope"])]
    pack = [item for item in request.get("research_pack", []) if isinstance(item, dict)]
    lines = [
        "✅ تم اختيار الفكرة وحفظ مصادر البحث",
        "",
        f"الموضوع: {request['approved_topic']}",
        f"النطاق: {scope_label}",
    ]
    if spacing_warning:
        lines.extend(["", spacing_warning])
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
            "إذا كان القرار نهائيًا اضغط زر «✅ تأكيد الإنتاج» أدناه،",
            f"أو أرسل حرفيًا:\n{CONFIRM_TEXT}",
        ]
    )
    return "\n".join(lines)


def selection_confirmation_keyboard(request: dict[str, Any]) -> list[list[dict[str, str]]]:
    request_id = str(request.get("request_id") or "").strip()
    if not request_id:
        raise RuntimeError("confirmation request id missing")
    return [
        [{"text": "✅ تأكيد الإنتاج", "callback_data": f"confirm:{request_id}"}],
        [{"text": "📚 المحاولات السابقة / الاستئناف", "callback_data": "main:saved"}],
        [{"text": "❌ إلغاء الاختيار", "callback_data": "main:cancel"}],
    ]


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
        "podcast": "🎙️ خارج النص",
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


def _github_release_json(path: str) -> Any:
    token = str(
        os.environ.get("GITHUB_RUNTIME_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or ""
    ).strip()
    repository = str(os.environ.get("GITHUB_REPOSITORY") or "").strip()
    api = str(os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    if not token or not repository:
        return None
    request = urllib.request.Request(
        f"{api}/repos/{repository}/{path.lstrip('/')}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "isco-clean-v2-last-delivery",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"Clean V2 release lookup failed: {type(exc).__name__}")
        return None


def _library_kind_for_scope(scope: str) -> str:
    value = str(scope or "")
    if value in {"long", "bundle"}:
        return "long"
    return value if value in {"short", "podcast"} else ""


def _release_library_records() -> list[dict[str, str]]:
    payload = _github_release_json("releases?per_page=100")
    if not isinstance(payload, list):
        return []
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for release in payload:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        tag = str(release.get("tag_name") or "")
        if not tag.startswith(CLEAN_V2_DELIVERY_TAG_PREFIX):
            continue
        kind = (
            "short"
            if tag.startswith(CLEAN_V2_DELIVERY_TAG_PREFIX + "short-")
            else ("podcast" if tag.startswith(CLEAN_V2_DELIVERY_TAG_PREFIX + "podcast-") else "long")
        )
        name = str(release.get("name") or "").strip()
        topic = name.split(" — ", 1)[1].strip() if " — " in name else ""
        key = kind + "|" + normalize_title(topic)
        if not topic or key in seen:
            continue
        seen.add(key)
        records.append(
            {
                "kind": kind,
                "topic": topic,
                "used_at": str(release.get("published_at") or release.get("created_at") or ""),
            }
        )
    return records


def _saved_library_items(
    state: dict[str, Any],
    kind: str,
    used_records: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    used_records = used_records if used_records is not None else _release_library_records()
    used_titles = [
        str(item.get("topic") or "")
        for item in used_records
        if str(item.get("kind") or "") == kind
    ]
    sessions = [
        item for item in state.get("sessions", {}).values()
        if isinstance(item, dict)
        and str(item.get("source") or "") != "saved_library"
        and _library_kind_for_scope(str(item.get("scope") or "")) == kind
    ]
    sessions.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for session in sessions:
        for rank, idea_id in enumerate(session.get("idea_ids") or [], 1):
            idea = _idea_by_id(state, str(idea_id))
            if not isinstance(idea, dict):
                continue
            title = str(idea.get("title") or "").strip()
            normalized = normalize_title(title)
            if not title or not normalized or normalized in seen:
                continue
            if any(same_topic(title, used_title) for used_title in used_titles):
                continue
            seen.add(normalized)
            result.append(
                {
                    "idea_id": str(idea.get("idea_id") or ""),
                    "title": title,
                    "scope": str(session.get("scope") or ""),
                    "rank": rank,
                }
            )
    return result


def _library_menu(
    state: dict[str, Any],
    bucket: str,
) -> tuple[str, list[list[dict[str, str]]]]:
    used = _release_library_records()
    if bucket == "saved":
        counts = {kind: len(_saved_library_items(state, kind, used)) for kind in LIBRARY_ORDER}
        lines = ["📚 المحفوظات", "", "من نتائج البحث التي عُرضت لك فعليًا؛ الأحدث أولًا."]
    elif bucket == "used":
        counts = {kind: sum(item.get("kind") == kind for item in used) for kind in LIBRARY_ORDER}
        lines = ["✅ المستعملة", "", "المواضيع التي خرج لها إنتاج ناجح فعليًا."]
    else:
        raise RuntimeError("unsupported library bucket")
    keyboard: list[list[dict[str, str]]] = []
    for kind in LIBRARY_ORDER:
        icon, label = LIBRARY_LABELS[kind]
        lines.append(f"{icon} {label} — {counts[kind]}")
        keyboard.append(
            [{"text": f"{icon} {label} ({counts[kind]})", "callback_data": f"library:{bucket}:{kind}"}]
        )
    keyboard.append([{"text": "↩️ الرئيسية", "callback_data": "main:home"}])
    return "\n".join(lines), keyboard


def _saved_library_view(
    state: dict[str, Any],
    kind: str,
) -> tuple[str, list[list[dict[str, str]]]]:
    icon, label = LIBRARY_LABELS[kind]
    items = _saved_library_items(state, kind)
    lines = [f"📚 المحفوظات — {icon} {label}", ""]
    keyboard: list[list[dict[str, str]]] = []
    if not items:
        lines.append("لا توجد أفكار محفوظة من البحث لهذا النوع حتى الآن.")
    else:
        lines.append("الأحدث أولًا، وداخل كل بحث يبقى ترتيب 1 ثم 2 ثم 3.")
        for item in items[:30]:
            rank = int(item["rank"])
            prefix = "1️⃣" if rank == 1 else "2️⃣" if rank == 2 else "3️⃣" if rank == 3 else "•"
            title = str(item["title"])
            short_title = title if len(title) <= 42 else title[:39].rstrip() + "…"
            keyboard.append(
                [{
                    "text": f"{prefix} {short_title}",
                    "callback_data": f"savedpick:{item['scope']}:{item['idea_id']}",
                }]
            )
        if len(items) > 30:
            lines.append(f"\n+ {len(items) - 30} أقدم محفوظة غير معروضة هنا.")
    keyboard.append([{"text": "↩️ المحفوظات", "callback_data": "library:saved"}])
    return "\n".join(lines), keyboard


def _used_library_view(kind: str) -> tuple[str, list[list[dict[str, str]]]]:
    icon, label = LIBRARY_LABELS[kind]
    items = [item for item in _release_library_records() if item.get("kind") == kind]
    lines = [f"✅ المستعملة — {icon} {label}", ""]
    if not items:
        lines.append("لا يوجد إنتاج ناجح لهذا النوع حتى الآن.")
    else:
        for index, item in enumerate(items[:30], 1):
            date = str(item.get("used_at") or "")[:10]
            lines.append(f"{index}) {str(item.get('topic') or '')}" + (f" — {date}" if date else ""))
        if len(items) > 30:
            lines.append(f"\n+ {len(items) - 30} أقدم.")
    return "\n".join(lines), [[{"text": "↩️ المستعملة", "callback_data": "library:used"}]]


def _history_current_runner_sha() -> str:
    return str(
        os.environ.get("CURRENT_RUNNER_SHA")
        or os.environ.get("GITHUB_SHA")
        or ""
    ).strip()


def _history_current_engine_sha() -> str:
    return str(os.environ.get("ISCO_ENGINE_SHA") or "").strip()


def _history_scope_label(scope: str) -> str:
    return {
        "long": "🎬 فيديو طويل",
        "bundle": "🎬 فيديو طويل",
        "short": "⚡ شورت",
        "podcast": "🎙️ خارج النص",
    }.get(str(scope or ""), "إنتاج")


def _history_topic_published(
    request: dict[str, Any],
    used_records: list[dict[str, str]],
) -> bool:
    kind = _library_kind_for_scope(str(request.get("scope") or ""))
    topic = str(request.get("approved_topic") or "").strip()
    return bool(
        kind
        and topic
        and any(
            str(item.get("kind") or "") == kind
            and same_topic(topic, str(item.get("topic") or ""))
            for item in used_records
        )
    )


def _history_items_by_kind(
    state: dict[str, Any],
    kind: str,
    used_records: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Newest unfinished production request per topic, scoped to one content kind."""
    if kind not in LIBRARY_ORDER:
        raise RuntimeError("unsupported history kind")
    used_records = used_records if used_records is not None else _release_library_records()
    result: list[dict[str, Any]] = []
    seen_topics: set[str] = set()
    for request in resume_history.incomplete_requests(state):
        if _library_kind_for_scope(str(request.get("scope") or "")) != kind:
            continue
        if _history_topic_published(request, used_records):
            continue
        title = str(request.get("approved_topic") or "").strip()
        request_id = str(request.get("request_id") or "").strip()
        normalized = normalize_title(title)
        if not title or not request_id or not normalized or normalized in seen_topics:
            continue
        # incomplete_requests() is newest-first, so the first duplicate is the
        # current request for that topic. Older retries stay in state for audit
        # history but do not clutter Telegram navigation.
        seen_topics.add(normalized)
        result.append(request)
    return result


def _history_view(
    state: dict[str, Any],
) -> tuple[str, list[list[dict[str, str]]]]:
    used_records = _release_library_records()
    counts = {
        kind: len(_history_items_by_kind(state, kind, used_records))
        for kind in LIBRARY_ORDER
    }
    lines = [
        "📚 المحفوظات",
        "",
        "اختر نوع المحتوى، ثم اختر الموضوع لعرض خيارات الاستئناف أو البدء من جديد.",
        "",
    ]
    keyboard: list[list[dict[str, str]]] = []
    for kind in LIBRARY_ORDER:
        icon, label = LIBRARY_LABELS[kind]
        lines.append(f"{icon} {label} — {counts[kind]}")
        keyboard.append(
            [{
                "text": f"{icon} {label} ({counts[kind]})",
                "callback_data": f"historyscope:{kind}",
            }]
        )
    keyboard.append(
        [{"text": "💡 أفكار البحث المحفوظة", "callback_data": "library:saved"}]
    )
    keyboard.append([{"text": "↩️ الرئيسية", "callback_data": "main:home"}])
    return "\n".join(lines), keyboard


def _history_scope_view(
    state: dict[str, Any],
    kind: str,
) -> tuple[str, list[list[dict[str, str]]]]:
    if kind not in LIBRARY_ORDER:
        raise RuntimeError("unsupported history kind")
    icon, label = LIBRARY_LABELS[kind]
    items = _history_items_by_kind(state, kind)
    lines = [
        f"📚 المحفوظات — {icon} {label}",
        "",
        "اختر الموضوع لعرض حالته وخياراته الحالية.",
    ]
    keyboard: list[list[dict[str, str]]] = []
    if not items:
        lines.extend(["", "لا توجد محاولات غير مكتملة لهذا النوع حاليًا."])
    else:
        for request in items[:30]:
            title = str(request.get("approved_topic") or "").strip()
            request_id = str(request.get("request_id") or "").strip()
            status = resume_history.request_status_label(request)
            short_title = title if len(title) <= 42 else title[:39].rstrip() + "…"
            lines.append(f"• {title} — {status}")
            keyboard.append(
                [{
                    "text": f"📌 {short_title}",
                    "callback_data": f"history:{request_id}",
                }]
            )
        if len(items) > 30:
            lines.append(f"\n+ {len(items) - 30} موضوعًا أقدم غير معروض.")
    keyboard.append([{"text": "↩️ المحفوظات", "callback_data": "main:saved"}])
    return "\n".join(lines), keyboard


def _resume_decision_for_request(request: dict[str, Any]) -> dict[str, Any]:
    stored_sha = str(request.get("request_sha256") or "")
    if not stored_sha or stored_sha != _request_hash(request):
        return {
            "available": False,
            "reason": "هوية الطلب الأصلية لا تطابق request_sha256 المحفوظ.",
            "completed_stage": "",
            "stage_label": "",
            "run_id": str((request.get("production") or {}).get("run_id") or ""),
        }
    if _history_topic_published(request, _release_library_records()):
        return {
            "available": False,
            "reason": "هذا الموضوع منشور نهائيًا بالفعل.",
            "completed_stage": "",
            "stage_label": "",
            "run_id": str((request.get("production") or {}).get("run_id") or ""),
        }
    return resume_history.evaluate_resume(
        request,
        current_runner_sha=_history_current_runner_sha(),
        current_engine_sha=_history_current_engine_sha(),
        github_json=resume_history.github_json,
        github_bytes=resume_history.github_bytes,
    )


def _history_request_view(
    state: dict[str, Any],
    request_id: str,
) -> tuple[str, list[list[dict[str, str]]]]:
    request = state.get("requests", {}).get(request_id)
    if not isinstance(request, dict):
        raise RuntimeError("history request is missing")
    topic = str(request.get("approved_topic") or "").strip()
    lines = [
        "📌 طلب غير مكتمل",
        "",
        f"الموضوع: {topic}",
        f"النوع: {_history_scope_label(str(request.get('scope') or ''))}",
        f"الحالة: {resume_history.request_status_label(request)}",
    ]
    decision = _resume_decision_for_request(request)
    keyboard: list[list[dict[str, str]]] = []
    if decision.get("available") is True:
        stage_label = str(decision.get("stage_label") or decision.get("completed_stage") or "")
        lines.extend(
            [
                "",
                "✅ الاستئناف متاح",
                f"سيستكمل من: {stage_label}",
                f"GitHub Run الأصلي: #{decision.get('run_id')}",
            ]
        )
        keyboard.append(
            [{
                "text": f"▶️ استئناف من {stage_label}",
                "callback_data": f"resume:{request_id}",
            }]
        )
    else:
        lines.extend(
            [
                "",
                "⛔ الاستئناف غير متاح",
                f"السبب: {str(decision.get('reason') or 'تعذر إثبات checkpoint صالح.')}",
                "الخيار الفعّال الوحيد: بدء طلب جديد من الصفر.",
            ]
        )
    keyboard.append(
        [{"text": "🔁 إعادة المحاولة من البداية", "callback_data": f"restart:{request_id}"}]
    )
    kind = _library_kind_for_scope(str(request.get("scope") or ""))
    back_callback = f"historyscope:{kind}" if kind in LIBRARY_ORDER else "main:saved"
    back_label = (
        f"↩️ {LIBRARY_LABELS[kind][1]}"
        if kind in LIBRARY_ORDER
        else "↩️ المحفوظات"
    )
    keyboard.append([{"text": back_label, "callback_data": back_callback}])
    return "\n".join(lines), keyboard


def restart_request_from_history(
    state: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
    old = state.get("requests", {}).get(request_id)
    if not isinstance(old, dict):
        raise RuntimeError("history request is missing")
    if (
        not old.get("request_sha256")
        or old.get("request_sha256") != _request_hash(old)
    ):
        raise RuntimeError("history request integrity mismatch")
    production = old.get("production")
    if isinstance(production, dict) and production.get("final_published") is True:
        raise RuntimeError("published request cannot be restarted from incomplete history")
    pack = [dict(item) for item in old.get("research_pack", []) if isinstance(item, dict)]
    if not pack:
        raise RuntimeError("history request has no research pack")
    new_id = "req-" + secrets.token_hex(6)
    request: dict[str, Any] = {
        "schema_version": 1,
        "request_id": new_id,
        "source": str(old.get("source") or "clean_v2_telegram_editorial_lite"),
        "scope": str(old.get("scope") or ""),
        "approved_by_user": True,
        "approved_topic": str(old.get("approved_topic") or ""),
        "research_pack": pack,
        "idea_id": str(old.get("idea_id") or ""),
        "selected_at": utc_now(),
        "status": "awaiting_confirmation",
        "confirmed_at": None,
        "dispatched_at": None,
    }
    if request["scope"] not in SCOPES or not request["approved_topic"]:
        raise RuntimeError("history request cannot be restarted safely")
    request["request_sha256"] = _request_hash(request)
    state["requests"][new_id] = request
    state["current_request_id"] = new_id
    return request


def request_resume_rerun(
    state: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
    request = state.get("requests", {}).get(request_id)
    if not isinstance(request, dict):
        raise RuntimeError("history request is missing")
    decision = _resume_decision_for_request(request)
    if decision.get("available") is not True:
        return decision
    run_id = str(decision.get("run_id") or "").strip()
    if not run_id:
        raise RuntimeError("resume decision is missing original run id")
    resume_history.rerun_workflow(run_id)
    production = request.get("production")
    if not isinstance(production, dict):
        raise RuntimeError("resume request production metadata is missing")
    production["last_job_status"] = "rerun_requested"
    production["resume_requested_at"] = utc_now()
    production["resume_requested_from_stage"] = str(decision.get("completed_stage") or "")
    return decision


def select_saved_candidate(state: dict[str, Any], scope: str, idea_id: str) -> dict[str, Any]:
    if scope not in SCOPES:
        raise RuntimeError("unsupported saved selection scope")
    idea = _idea_by_id(state, idea_id)
    if not isinstance(idea, dict):
        raise RuntimeError("saved idea is missing")
    kind = _library_kind_for_scope(scope)
    used_titles = [
        str(item.get("topic") or "")
        for item in _release_library_records()
        if item.get("kind") == kind
    ]
    if any(same_topic(str(idea.get("title") or ""), title) for title in used_titles):
        raise RuntimeError("saved idea is already used")
    session_id = secrets.token_hex(4)
    state["sessions"][session_id] = {
        "session_id": session_id,
        "scope": scope,
        "idea_ids": [idea_id],
        "created_at": utc_now(),
        "source": "saved_library",
    }
    return select_candidate(state, session_id, 0)


def latest_release_delivery() -> dict[str, str]:
    payload = _github_release_json("releases?per_page=50")
    if not isinstance(payload, list):
        return {}
    for release in payload:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        tag = str(release.get("tag_name") or "")
        if not tag.startswith(CLEAN_V2_DELIVERY_TAG_PREFIX):
            continue
        assets = release.get("assets")
        if not isinstance(assets, list):
            continue
        direct_url = ""
        package_url = ""
        for item in assets:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            candidate = str(item.get("browser_download_url") or "").strip()
            if not candidate.startswith("https://"):
                continue
            if name == "final.mp4":
                direct_url = candidate
            elif name == "publish-package.zip":
                package_url = candidate
        if not direct_url and not package_url:
            continue
        kind = (
            "short"
            if tag.startswith(CLEAN_V2_DELIVERY_TAG_PREFIX + "short-")
            else ("podcast" if tag.startswith(CLEAN_V2_DELIVERY_TAG_PREFIX + "podcast-") else "long")
        )
        name = str(release.get("name") or "").strip()
        topic = name.split(" — ", 1)[1].strip() if " — " in name else ""
        return {
            "kind": kind,
            "topic": topic,
            "release_tag": tag,
            "browser_download_url": direct_url,
            "package_browser_download_url": package_url,
        }
    return {}


def render_last_success(delivery: dict[str, Any]) -> tuple[str, list[list[dict[str, str]]] | None]:
    if not isinstance(delivery, dict) or not delivery:
        return "⚪ لا يوجد إنتاج ناجح محفوظ بعد.", None
    kind = str(delivery.get("kind") or "")
    scope_label = "⚡ شورت" if kind == "short" else ("🎙️ خارج النص" if kind == "podcast" else "🎬 فيديو طويل")
    topic = str(delivery.get("topic") or "").strip()
    url = str(delivery.get("browser_download_url") or "").strip()
    package_url = str(delivery.get("package_browser_download_url") or "").strip()
    lines = ["✅ آخر إنتاج ناجح", f"النوع: {scope_label}"]
    if topic:
        lines.append(f"الموضوع: {topic}")
    rows: list[list[dict[str, str]]] = []
    if package_url:
        rows.append([{"text": "📦 تحميل حزمة النشر كاملة", "url": package_url}])
    if url:
        rows.append([{"text": "🎥 مشاهدة/تحميل الفيديو", "url": url}])
    return "\n".join(lines), (rows or None)


def authorized(update: dict[str, Any]) -> bool:
    expected = str(os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    actor, chat = _actor_chat(update)
    return bool(expected and actor == expected and chat == expected)


def _normalize_user_command_text(value: str) -> str:
    text = str(value or "")
    for marker in ("\u200e", "\u200f", "\u061c", "\ufe0f"):
        text = text.replace(marker, "")
    text = " ".join(text.split())
    while text and not (text[0].isalnum() or text[0] in {"/", "_"}):
        text = text[1:].lstrip()
    return text


def _confirm_and_notify(
    state: dict[str, Any],
    dispatch_path: Path,
    *,
    expected_request_id: str | None = None,
) -> None:
    try:
        result = _stage_confirmation_dispatch(
            state,
            dispatch_path,
            expected_request_id=expected_request_id,
        )
    except ProviderCooldownActive as cooldown:
        minutes = max(1, (cooldown.remaining_seconds + 59) // 60)
        send_telegram(
            "⏳ آخر محاولة فشلت قبل أن تبدأ الكتابة الفعلية (نفاد حدود المزودين: Gemini/Groq/OpenRouter في نفس الوقت).\n"
            f"إعادة الإرسال الآن ستهدر الحصة المتبقية بلا فائدة. انتظر نحو {minutes} دقيقة ثم أعد الضغط على تأكيد."
        )
        return
    except Exception:
        if expected_request_id is not None:
            send_telegram(
                "⚠️ زر التأكيد هذا قديم أو لا يطابق الاختيار الحالي. "
                "افتح آخر رسالة «تم اختيار الفكرة» وحاول منها."
            )
        else:
            send_telegram("⚠️ لا يوجد اختيار صالح ينتظر التأكيد. اطلب /research واختر فكرة أولًا.")
        return
    request = result["request"]
    if result["already_dispatched"]:
        send_telegram("✅ هذا الطلب أُرسل للإنتاج بالفعل. لن أنشئ محاولة مكررة.")
        return
    send_telegram(
        "🚀 تم تأكيد الإنتاج. حُفظ القرار أولًا، وسيُرسل الآن إلى Clean V2.\n"
        f"الموضوع: {request['approved_topic']}"
    )


def handle_update(state: dict[str, Any], update: dict[str, Any], dispatch_path: Path) -> None:
    if not authorized(update):
        raise RuntimeError("unauthorized Telegram update")
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        data = str(callback.get("data") or "")
        if data.startswith("confirm:"):
            parts = data.split(":", 1)
            expected_request_id = parts[1].strip() if len(parts) == 2 else ""
            if not expected_request_id:
                send_telegram("⚠️ زر التأكيد غير صالح. افتح آخر رسالة اختيار وحاول مجددًا.")
                return
            _confirm_and_notify(
                state,
                dispatch_path,
                expected_request_id=expected_request_id,
            )
            return
        if data.startswith("main:"):
            action = data.split(":", 1)[1]
            if action == "home":
                send_telegram(render_main_menu(), main_menu_keyboard())
                return
            if action == "research":
                send_telegram(
                    "🔎 اختر نوع المحتوى الذي تريد البحث له. لن يبدأ الإنتاج قبل تأكيدك النهائي.",
                    scope_keyboard(),
                )
                return
            if action == "saved":
                history_text, history_keyboard = _history_view(state)
                send_telegram(history_text, history_keyboard)
                return
            if action == "used":
                library_text, library_keyboard = _library_menu(state, action)
                send_telegram(library_text, library_keyboard)
                return
            if action == "status":
                send_telegram(
                    render_production_status(load_runtime_status()),
                    [[{"text": "↩️ الرئيسية", "callback_data": "main:home"}]],
                )
                return
            if action == "last":
                last_text, last_keyboard = render_last_success(latest_release_delivery())
                rows = list(last_keyboard or [])
                rows.append([{"text": "↩️ الرئيسية", "callback_data": "main:home"}])
                send_telegram(last_text, rows)
                return
            if action == "stats":
                try:
                    stats = channel_stats(state)
                except Exception as exc:
                    print(f"YouTube stats failed: {type(exc).__name__}")
                    send_telegram(
                        "⚠️ تعذر تحديث إحصائيات YouTube الآن. لم يتأثر البحث أو الإنتاج.",
                        [[{"text": "↩️ الرئيسية", "callback_data": "main:home"}]],
                    )
                    return
                send_telegram(
                    render_channel_stats(stats),
                    [[{"text": "↩️ الرئيسية", "callback_data": "main:home"}]],
                )
                return
            if action == "cancel":
                try:
                    request = cancel_current(state)
                except Exception:
                    send_telegram(
                        "⚠️ لا يوجد اختيار معلّق يمكن إلغاؤه الآن.",
                        [[{"text": "↩️ الرئيسية", "callback_data": "main:home"}]],
                    )
                    return
                send_telegram(
                    f"🛑 تم إلغاء الاختيار المعلّق:\n{request['approved_topic']}\n\nلم يبدأ أي إنتاج.",
                    [[{"text": "↩️ الرئيسية", "callback_data": "main:home"}]],
                )
                return
            raise RuntimeError("unsupported main-menu callback")
        if data.startswith("historyscope:"):
            kind = data.split(":", 1)[1].strip()
            try:
                if kind not in LIBRARY_ORDER:
                    raise RuntimeError("malformed history scope")
                history_text, history_keyboard = _history_scope_view(state, kind)
            except Exception:
                send_telegram(
                    "⚠️ تعذر فتح محفوظات هذا النوع الآن.",
                    [[{"text": "↩️ المحفوظات", "callback_data": "main:saved"}]],
                )
                return
            send_telegram(history_text, history_keyboard)
            return
        if data.startswith("history:"):
            request_id = data.split(":", 1)[1].strip()
            try:
                history_text, history_keyboard = _history_request_view(state, request_id)
            except Exception:
                send_telegram(
                    "⚠️ تعذر فتح سجل هذا الطلب الآن.",
                    [[{"text": "↩️ المحفوظات", "callback_data": "main:saved"}]],
                )
                return
            send_telegram(history_text, history_keyboard)
            return
        if data.startswith("resume:"):
            request_id = data.split(":", 1)[1].strip()
            try:
                decision = request_resume_rerun(state, request_id)
            except Exception as exc:
                print(f"Telegram resume rerun failed: {type(exc).__name__}")
                send_telegram(
                    "⚠️ لم يقبل GitHub طلب الاستئناف. لم يبدأ أي تشغيل جديد.",
                    [[{"text": "↩️ المحفوظات", "callback_data": "main:saved"}]],
                )
                return
            if decision.get("available") is not True:
                send_telegram(
                    "⛔ الاستئناف لم يعد متاحًا.\n"
                    f"السبب: {str(decision.get('reason') or 'checkpoint لم يعد صالحًا.')}",
                    [[{"text": "↩️ المحفوظات", "callback_data": "main:saved"}]],
                )
                return
            send_telegram(
                "▶️ تم طلب الاستئناف اليدوي لنفس GitHub Run الأصلي.\n"
                f"سيستكمل من: {str(decision.get('stage_label') or decision.get('completed_stage') or '')}\n"
                f"Run: #{str(decision.get('run_id') or '')}"
            )
            return
        if data.startswith("restart:"):
            request_id = data.split(":", 1)[1].strip()
            try:
                request = restart_request_from_history(state, request_id)
            except Exception:
                send_telegram(
                    "⚠️ تعذر إنشاء طلب جديد من هذا السجل.",
                    [[{"text": "↩️ المحفوظات", "callback_data": "main:saved"}]],
                )
                return
            send_telegram(
                render_selection_confirmation(
                    request,
                    spacing_warning=_safe_publication_spacing_warning(request),
                ),
                selection_confirmation_keyboard(request),
            )
            return
        if data in {"library:saved", "library:used"}:
            bucket = data.split(":", 1)[1]
            library_text, library_keyboard = _library_menu(state, bucket)
            send_telegram(library_text, library_keyboard)
            return
        if data.startswith("library:saved:") or data.startswith("library:used:"):
            parts = data.split(":")
            try:
                if len(parts) != 3 or parts[2] not in LIBRARY_ORDER:
                    raise RuntimeError("malformed library callback")
                if parts[1] == "saved":
                    library_text, library_keyboard = _saved_library_view(state, parts[2])
                else:
                    library_text, library_keyboard = _used_library_view(parts[2])
            except Exception:
                send_telegram("⚠️ تعذر فتح هذه القائمة الآن.")
                return
            send_telegram(library_text, library_keyboard)
            return
        if data.startswith("savedpick:"):
            parts = data.split(":")
            try:
                if len(parts) != 3:
                    raise RuntimeError("malformed saved selection")
                request = select_saved_candidate(state, parts[1], parts[2])
            except Exception:
                send_telegram("⚠️ هذه الفكرة المحفوظة لم تعد صالحة للاختيار.")
                return
            send_telegram(
                render_selection_confirmation(
                    request,
                    spacing_warning=_safe_publication_spacing_warning(request),
                ),
                selection_confirmation_keyboard(request),
            )
            return
        if data.startswith("scope:"):
            scope = data.split(":", 1)[1]
            try:
                result = research(state, scope)
            except Exception as exc:
                print(f"Telegram research failed: {type(exc).__name__}")
                reason = research_failure_reason()
                send_telegram(
                    "⚠️ لم يُعثر على مواضيع مناسبة بهذه المعايير الآن. "
                    + (f"السبب: {reason} " if reason else "")
                    + "لم يبدأ أي إنتاج؛ جرّب معايير مختلفة أو أعد البحث لاحقًا."
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
            send_telegram(
                render_selection_confirmation(
                    request,
                    spacing_warning=_safe_publication_spacing_warning(request),
                ),
                selection_confirmation_keyboard(request),
            )
            return
        raise RuntimeError("unsupported callback")

    message = update.get("message") or {}
    text = _normalize_user_command_text(str(message.get("text") or ""))
    if text in {"/start", "start", "/menu", "menu", "ابدأ", "ابدأ البوت", "الرئيسية"}:
        send_telegram(render_main_menu(), main_menu_keyboard())
        return
    if text in {"/research", "research", "بحث", "بحث جديد"}:
        send_telegram(
            "🔎 اختر نوع المحتوى الذي تريد البحث له. لن يبدأ الإنتاج قبل تأكيدك النهائي.",
            scope_keyboard(),
        )
        return
    if text in {"/saved", "saved", "محفوظات", "المحفوظات"}:
        history_text, history_keyboard = _history_view(state)
        send_telegram(history_text, history_keyboard)
        return
    if text in {"/used", "used", "مستعملة", "المستعملة"}:
        library_text, library_keyboard = _library_menu(state, "used")
        send_telegram(library_text, library_keyboard)
        return
    if text in {"/status", "status", "الحالة", "حالة الإنتاج", "حاله الانتاج"}:
        send_telegram(render_production_status(load_runtime_status()))
        return
    if text in {"/last", "last", "آخر إنتاج", "اخر انتاج"}:
        last_text, last_keyboard = render_last_success(latest_release_delivery())
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
    if text in {"/cancel", "cancel", "إلغاء", "الغاء", "إلغاء الاختيار"}:
        try:
            request = cancel_current(state)
        except Exception:
            send_telegram("⚠️ لا يوجد اختيار معلّق يمكن إلغاؤه الآن.")
            return
        send_telegram(f"🛑 تم إلغاء الاختيار المعلّق:\n{request['approved_topic']}\n\nلم يبدأ أي إنتاج.")
        return
    if text == CONFIRM_TEXT:
        _confirm_and_notify(state, dispatch_path)
        return
    send_telegram(
        "استخدم /research للبحث، /saved للمحفوظات، /used للمستعملة، و/stats للإحصائيات. "
        "بدء الإنتاج يتطلب تأكيدًا منفصلًا من زر الطلب أو إرسال «تأكيد الإنتاج» حرفيًا."
    )


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
            "برنامج خارج النص: حوار listener-proxy ثابت بالعربية الفصحى الطبيعية وبصوت القناة الثابت، ويقدّم حوارًا حقيقيًا مع مستمع واحد. "
            "A بصوت Orus يمثل ذلك المستمع بسؤال أو اعتراض قصير ومحدد عند الحاجة فقط، وB بصوت Charon هو صوت القناة ويحمل الشرح الأساسي. "
            "يبدأ الموضوع بسؤال مركزي حقيقي، ثم يجيب B على نفس التوتر مباشرة بعد هوية البرنامج ويتقدم طبقة بعد طبقة "
            "حتى يتغير فهم المستمع. لا مضيف/ضيف، لا مجاملات، لا تناوب آلي، لا قائمة نصائح، ولا محاضرة؛ "
            "الحلقة يجب أن تبقى مفهومة وممتعة صوتيًا دون الصورة."
            if fmt == "podcast"
            else "محتوى عربي فصيح طبيعي، متفائل وواقعي، واضح ومفيد، "
            "مع تجنب المبالغة والادعاءات غير المدعومة."
        ),
        "research_pack": list(request.get("research_pack") or []),
        "hard_constraints": [
            "No fabricated facts.",
            "Use research_pack only within each source claim_scope.",
            "Gemini 3.8 is the only voice provider: Charon is the primary narrator; Orus is allowed only when Planning selects dialogue_qa.",
            *(
                [
                    "Outside Text uses fixed listener-proxy dialogue: Orus is A (the sparse listener question/objection) and Charon is B (the channel voice carrying the answer).",
                    "Every A turn must unlock a genuinely new layer and receive an immediate B answer; never use A as a host, interviewer, or filler speaker.",
                    "Outside Text must stay conversational and simple-deep; it must not become a monologue, host/guest interview, lecture, or numbered-list episode, and must never invent first-person experiences.",
                    "Selected visuals must remain modest and respectful for a broad Arab/Muslim audience.",
                ]
                if fmt == "podcast"
                else []
            ),
        ],
    }
    if fmt == "podcast":
        brief["series_name"] = "خارج النص"
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
    brief.add_argument("--format", choices=("film", "short", "podcast"), required=True)
    brief.add_argument("--output", type=Path, required=True)

    record_start = sub.add_parser("record-production-start")
    record_start.add_argument("--state", type=Path, required=True)
    record_start.add_argument("--request-id", required=True)
    record_start.add_argument("--request-sha256", required=True)
    record_start.add_argument("--run-id", required=True)
    record_start.add_argument("--run-attempt", required=True)
    record_start.add_argument("--run-url", required=True)
    record_start.add_argument("--runner-sha", required=True)
    record_start.add_argument("--engine-sha", required=True)
    record_start.add_argument("--resume-cache-key", required=True)

    record_terminal = sub.add_parser("record-production-terminal")
    record_terminal.add_argument("--state", type=Path, required=True)
    record_terminal.add_argument("--request-id", required=True)
    record_terminal.add_argument("--request-sha256", required=True)
    record_terminal.add_argument("--run-id", required=True)
    record_terminal.add_argument("--run-attempt", required=True)
    record_terminal.add_argument("--job-status", required=True)
    record_terminal.add_argument("--manifest", type=Path, required=True)
    record_terminal.add_argument("--artifact-id", default="")
    record_terminal.add_argument("--artifact-name", default="")
    record_terminal.add_argument("--artifact-url", default="")
    record_terminal.add_argument("--final-published", choices=("true", "false"), required=True)

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
    if args.command == "record-production-start":
        request = state.get("requests", {}).get(args.request_id)
        if (
            not isinstance(request, dict)
            or request.get("request_sha256") != args.request_sha256
            or args.request_sha256 != _request_hash(request)
        ):
            raise RuntimeError("production history request integrity mismatch")
        updated = resume_history.record_production_start(
            state,
            request_id=args.request_id,
            request_sha256=args.request_sha256,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            run_url=args.run_url,
            runner_sha=args.runner_sha,
            engine_sha=args.engine_sha,
            resume_cache_key=args.resume_cache_key,
        )
        save_state(args.state, state)
        print(json.dumps({"request_id": updated["request_id"], "production": updated["production"]}, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "record-production-terminal":
        request = state.get("requests", {}).get(args.request_id)
        if (
            not isinstance(request, dict)
            or request.get("request_sha256") != args.request_sha256
            or args.request_sha256 != _request_hash(request)
        ):
            raise RuntimeError("production history request integrity mismatch")
        manifest_status = "missing"
        manifest_failure_classification = ""
        try:
            manifest_value = json.loads(args.manifest.read_text(encoding="utf-8"))
            if isinstance(manifest_value, dict):
                manifest_status = str(manifest_value.get("status") or "unknown")
                manifest_failure_classification = str(manifest_value.get("failure_classification") or "")
        except (OSError, ValueError, TypeError):
            pass
        resume_history.apply_provider_cooldown_if_exhausted(
            state, failure_classification=manifest_failure_classification
        )
        updated = resume_history.record_production_terminal(
            state,
            request_id=args.request_id,
            request_sha256=args.request_sha256,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            job_status=args.job_status,
            manifest_status=manifest_status,
            artifact_id=args.artifact_id,
            artifact_name=args.artifact_name,
            artifact_url=args.artifact_url,
            final_published=args.final_published == "true",
        )
        save_state(args.state, state)
        print(json.dumps({"request_id": updated["request_id"], "production": updated["production"]}, ensure_ascii=False, sort_keys=True))
        return 0
    materialize_brief(state, args.request_id, args.request_sha256, args.format, args.output)
    print(json.dumps({"request_id": args.request_id, "format": args.format, "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
