from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_CHANNEL_ID = "UC_fmWGRen6QUQNd4Dj80MgA"
SNAPSHOT_STATE_KEY = "youtube_stats_snapshots"
LAST_LIVE_STATE_KEY = "youtube_stats_last_live_at"
MAX_SNAPSHOTS = 240
MUSCAT = ZoneInfo("Asia/Muscat")
_DURATION_RE = re.compile(r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$")


def _channel_id() -> str:
    return (os.environ.get("YOUTUBE_CHANNEL_ID") or DEFAULT_CHANNEL_ID).strip()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _parse_time(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _duration_seconds(value: str) -> int:
    match = _DURATION_RE.match(str(value or ""))
    if not match:
        return 0
    parts = {name: int(number or 0) for name, number in match.groupdict().items()}
    return parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]


def _api_get(resource: str, params: dict[str, Any], api_key: str) -> dict[str, Any]:
    key = str(api_key or "").strip()
    if not key:
        raise RuntimeError("YOUTUBE_API_KEY is missing")
    query = dict(params)
    query["key"] = key
    url = f"https://www.googleapis.com/youtube/v3/{resource}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(url, headers={"User-Agent": "Isco-Video-Runner/telegram-stats-v1"})
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("YouTube API returned malformed JSON")
    return payload


def fetch_live(api_key: str, channel_id: str | None = None) -> dict[str, Any]:
    cid = str(channel_id or _channel_id()).strip()
    channel_payload = _api_get(
        "channels",
        {"part": "snippet,statistics,contentDetails", "id": cid, "maxResults": 1},
        api_key,
    )
    items = channel_payload.get("items") or []
    if not items or not isinstance(items[0], dict):
        raise RuntimeError("YouTube channel was not found")
    channel = items[0]
    stats = channel.get("statistics") or {}
    uploads = (((channel.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads") or "").strip()
    if not uploads:
        raise RuntimeError("YouTube uploads playlist is unavailable")

    playlist = _api_get(
        "playlistItems",
        {"part": "contentDetails,snippet", "playlistId": uploads, "maxResults": 25},
        api_key,
    )
    playlist_items = [item for item in (playlist.get("items") or []) if isinstance(item, dict)]
    ids = [str((item.get("contentDetails") or {}).get("videoId") or "").strip() for item in playlist_items]
    ids = [value for value in ids if value]
    videos: list[dict[str, Any]] = []
    if ids:
        video_payload = _api_get(
            "videos",
            {"part": "snippet,statistics,contentDetails", "id": ",".join(ids[:50]), "maxResults": 50},
            api_key,
        )
        for item in video_payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            snippet = item.get("snippet") or {}
            vstats = item.get("statistics") or {}
            duration = _duration_seconds(str((item.get("contentDetails") or {}).get("duration") or ""))
            published = str(snippet.get("publishedAt") or "")
            videos.append(
                {
                    "id": str(item.get("id") or ""),
                    "title": str(snippet.get("title") or "").strip(),
                    "published_at": published,
                    "duration_seconds": duration,
                    # YouTube Shorts can be up to 3 minutes. The public Data API does not expose
                    # a canonical Shorts flag, so this operational panel intentionally uses a
                    # duration-only approximation and labels the result as approximate.
                    "is_short_approx": 0 < duration <= 180,
                    "views": _int(vstats.get("viewCount")),
                    "likes": _int(vstats.get("likeCount")),
                    "comments": _int(vstats.get("commentCount")),
                }
            )
    videos.sort(key=lambda item: str(item.get("published_at") or ""), reverse=True)
    return {
        "fetched_at": _iso(_now()),
        "channel_id": cid,
        "channel_title": str((channel.get("snippet") or {}).get("title") or "").strip(),
        "hidden_subscriber_count": bool(stats.get("hiddenSubscriberCount", False)),
        "subscribers": _int(stats.get("subscriberCount")),
        "views": _int(stats.get("viewCount")),
        "videos_count": _int(stats.get("videoCount")),
        "videos": videos,
    }


def _snapshot(live: dict[str, Any]) -> dict[str, Any]:
    return {
        "at": str(live.get("fetched_at") or _iso(_now())),
        "subscribers": _int(live.get("subscribers")),
        "views": _int(live.get("views")),
        "videos_count": _int(live.get("videos_count")),
    }


def record_snapshot(state: dict[str, Any], live: dict[str, Any]) -> None:
    store = state.setdefault(SNAPSHOT_STATE_KEY, [])
    if not isinstance(store, list):
        store = []
        state[SNAPSHOT_STATE_KEY] = store
    current = _snapshot(live)
    current_at = _parse_time(current["at"]) or _now()
    if store:
        last = store[-1] if isinstance(store[-1], dict) else {}
        last_at = _parse_time(str(last.get("at") or ""))
        # Avoid noisy near-duplicate writes if the same workflow asks for more than one stats card.
        if last_at and abs((current_at - last_at).total_seconds()) < 60:
            store[-1] = current
        else:
            store.append(current)
    else:
        store.append(current)
    cutoff = current_at - timedelta(days=8)
    cleaned = [item for item in store if isinstance(item, dict) and (_parse_time(str(item.get("at") or "")) or current_at) >= cutoff]
    state[SNAPSHOT_STATE_KEY] = cleaned[-MAX_SNAPSHOTS:]
    state[LAST_LIVE_STATE_KEY] = current["at"]


def _format_num(value: int) -> str:
    number = max(0, int(value))
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}K"
    return f"{number:,}"


def _updated_text(live: dict[str, Any]) -> str:
    dt = _parse_time(str(live.get("fetched_at") or "")) or _now()
    return dt.astimezone(MUSCAT).strftime("%H:%M")


def _video_url(video_id: str) -> str:
    return f"https://youtu.be/{video_id}" if video_id else ""


def _latest(live: dict[str, Any], *, short: bool) -> dict[str, Any] | None:
    for video in live.get("videos") or []:
        if isinstance(video, dict) and bool(video.get("is_short_approx")) is short:
            return video
    return None


def render_latest(live: dict[str, Any], *, short: bool) -> tuple[str, str | None]:
    item = _latest(live, short=short)
    label = "Short" if short else "فيديو طويل"
    if item is None:
        return f"📈 آخر {label}\n\nلم أجد عنصرًا حديثًا مناسبًا ضمن آخر الرفعات.", None
    published = _parse_time(str(item.get("published_at") or ""))
    published_text = published.astimezone(MUSCAT).strftime("%d/%m %H:%M") if published else "غير معروف"
    text = (
        f"📈 آخر {label}\n\n"
        f"🎬 {item.get('title', '')}\n\n"
        f"👁️ {_format_num(_int(item.get('views')))} مشاهدة\n"
        f"👍 {_format_num(_int(item.get('likes')))} إعجاب\n"
        f"💬 {_format_num(_int(item.get('comments')))} تعليق\n"
        f"🕒 نُشر: {published_text}\n\n"
        f"🔄 تحديث: {_updated_text(live)} بتوقيت عُمان"
    )
    if short:
        text += "\nℹ️ تصنيف Short هنا تقريبي بالمدة (≤3 دقائق)."
    return text, _video_url(str(item.get("id") or "")) or None


def render_overview(live: dict[str, Any]) -> str:
    subscribers = "مخفية" if live.get("hidden_subscriber_count") else _format_num(_int(live.get("subscribers")))
    return (
        "📊 إحصائيات عامة\n\n"
        f"👥 المشتركون: {subscribers}\n"
        f"👁️ مشاهدات القناة: {_format_num(_int(live.get('views')))}\n"
        f"🎞️ إجمالي الفيديوهات: {_format_num(_int(live.get('videos_count')))}\n\n"
        f"🔄 تحديث: {_updated_text(live)} بتوقيت عُمان\n"
        "ℹ️ لوحة تشغيل سريعة وليست بديلًا عن YouTube Studio."
    )


def _period_start(days: int, now_utc: datetime) -> datetime:
    local = now_utc.astimezone(MUSCAT)
    if days <= 1:
        local_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        local_start = (local - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return local_start.astimezone(timezone.utc)


def _baseline(state: dict[str, Any], start: datetime) -> dict[str, Any] | None:
    snapshots = [item for item in state.get(SNAPSHOT_STATE_KEY, []) if isinstance(item, dict)]
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for item in snapshots:
        at = _parse_time(str(item.get("at") or ""))
        if at and at <= start:
            candidates.append((at, item))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


def render_period(live: dict[str, Any], state: dict[str, Any], *, days: int) -> str:
    now_utc = _parse_time(str(live.get("fetched_at") or "")) or _now()
    start = _period_start(days, now_utc)
    new_videos = 0
    new_shorts = 0
    for item in live.get("videos") or []:
        if not isinstance(item, dict):
            continue
        published = _parse_time(str(item.get("published_at") or ""))
        if published and published >= start:
            new_videos += 1
            if item.get("is_short_approx"):
                new_shorts += 1
    baseline = _baseline(state, start)
    title = "اليوم" if days <= 1 else "آخر 7 أيام"
    lines = [f"📈 {title}", ""]
    if baseline is None:
        lines.append("⏳ خط الأساس ما زال يتكوّن؛ سأبدأ حساب الفروق تلقائيًا من اللقطات المحفوظة.")
    else:
        lines.append(f"👁️ فرق مشاهدات القناة: +{_format_num(max(0, _int(live.get('views')) - _int(baseline.get('views'))))}")
        if not live.get("hidden_subscriber_count"):
            lines.append(f"👥 فرق المشتركين: +{_format_num(max(0, _int(live.get('subscribers')) - _int(baseline.get('subscribers'))))}")
        lines.append(f"🎞️ فرق عدد الفيديوهات: +{_format_num(max(0, _int(live.get('videos_count')) - _int(baseline.get('videos_count'))))}")
    lines.extend(
        [
            f"🆕 رفعات ضمن الفترة: {new_videos}",
            f"⚡ منها Shorts تقريبًا: {new_shorts}",
            "",
            f"🔄 تحديث: {_updated_text(live)} بتوقيت عُمان",
            "ℹ️ هذه مؤشرات تشغيلية سريعة وليست YouTube Analytics الدقيقة لكل ساعة.",
        ]
    )
    return "\n".join(lines)
