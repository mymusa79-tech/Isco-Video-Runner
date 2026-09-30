from __future__ import annotations

"""Small, zero-cost YouTube learning loop for Clean V2.

This module deliberately stays outside Production authority:
- refreshes at most once per day (failed attempts retry after six hours)
- reads only the latest few published channel videos
- stores compact observational metrics in the already-encrypted control state
- emits only descriptive channel-learning signals
- never changes gates, scores, voice, visuals, or publishing automatically
"""

import argparse
import json
import math
import os
import statistics
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from scripts import telegram_youtube_stats


STATE_KEY = "youtube_learning_lite"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ANALYTICS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
REFRESH_HOURS = 24
ERROR_RETRY_HOURS = 6
MAX_VIDEOS = 8
MAX_SAMPLES = 18
MIN_FORMAT_SAMPLES = 3
TIMEOUT_SECONDS = 20


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


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


def _safe_float(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _post_form(url: str, data: dict[str, str], *, timeout: int = TIMEOUT_SECONDS) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Isco-Video-Runner/youtube-learning-lite",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("OAuth token response was not an object")
    return payload


def _get_json(
    url: str,
    *,
    params: dict[str, object],
    access_token: str,
    timeout: int = TIMEOUT_SECONDS,
) -> dict[str, Any]:
    request_url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        request_url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "Isco-Video-Runner/youtube-learning-lite",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("YouTube Analytics response was not an object")
    return payload


def _refresh_access_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    *,
    post_form: Callable[..., dict[str, Any]] = _post_form,
) -> str:
    payload = post_form(
        TOKEN_URL,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=TIMEOUT_SECONDS,
    )
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("OAuth refresh returned no access token")
    return token


def _rows_as_dicts(report: dict[str, Any]) -> list[dict[str, object]]:
    headers = report.get("columnHeaders")
    rows = report.get("rows")
    if not isinstance(headers, list) or not isinstance(rows, list):
        return []
    names = [
        str(item.get("name") or "")
        for item in headers
        if isinstance(item, dict)
    ]
    if not names:
        return []
    result: list[dict[str, object]] = []
    for row in rows:
        if isinstance(row, list):
            result.append(dict(zip(names, row)))
    return result


def _date_window(published_at: object, now: datetime) -> tuple[str, str] | None:
    published = _parse_time(published_at)
    end = now.date() - timedelta(days=1)
    if published is not None:
        start = max(published.date(), end - timedelta(days=365))
    else:
        start = end - timedelta(days=365)
    if start > end:
        return None
    return start.isoformat(), end.isoformat()


def _base_metrics(
    access_token: str,
    video: dict[str, Any],
    *,
    now: datetime,
    get_json: Callable[..., dict[str, Any]] = _get_json,
) -> dict[str, float]:
    window = _date_window(video.get("published_at"), now)
    if window is None:
        return {}
    start_date, end_date = window
    report = get_json(
        ANALYTICS_URL,
        params={
            "ids": "channel==MINE",
            "startDate": start_date,
            "endDate": end_date,
            "metrics": (
                "views,estimatedMinutesWatched,averageViewDuration,"
                "averageViewPercentage,subscribersGained"
            ),
            "filters": f"video=={str(video.get('id') or '')}",
        },
        access_token=access_token,
        timeout=TIMEOUT_SECONDS,
    )
    rows = _rows_as_dicts(report)
    if not rows:
        return {}
    row = rows[0]
    mapping = {
        "views": "views",
        "averageViewDuration": "avg_view_duration_seconds",
        "averageViewPercentage": "avg_percentage_viewed",
        "subscribersGained": "subscribers_gained",
    }
    clean: dict[str, float] = {}
    for source, target in mapping.items():
        value = _safe_float(row.get(source))
        if value is not None:
            clean[target] = value
    minutes = _safe_float(row.get("estimatedMinutesWatched"))
    if minutes is not None:
        clean["watch_time_hours"] = minutes / 60.0
    return clean


def _retention_points(
    access_token: str,
    video: dict[str, Any],
    *,
    now: datetime,
    get_json: Callable[..., dict[str, Any]] = _get_json,
) -> list[tuple[float, float]]:
    window = _date_window(video.get("published_at"), now)
    if window is None:
        return []
    start_date, end_date = window
    report = get_json(
        ANALYTICS_URL,
        params={
            "ids": "channel==MINE",
            "startDate": start_date,
            "endDate": end_date,
            "dimensions": "elapsedVideoTimeRatio",
            "metrics": "audienceWatchRatio",
            "filters": f"video=={str(video.get('id') or '')}",
            "sort": "elapsedVideoTimeRatio",
            "maxResults": 200,
        },
        access_token=access_token,
        timeout=TIMEOUT_SECONDS,
    )
    points: list[tuple[float, float]] = []
    for row in _rows_as_dicts(report):
        ratio = _safe_float(row.get("elapsedVideoTimeRatio"))
        watch = _safe_float(row.get("audienceWatchRatio"))
        if ratio is None or watch is None or ratio < 0.0 or ratio > 1.0 or watch < 0.0:
            continue
        points.append((ratio, watch))
    points.sort(key=lambda pair: pair[0])
    return points


def _retention_summary(points: list[tuple[float, float]], duration_seconds: float) -> dict[str, object]:
    if not points:
        return {}
    result: dict[str, object] = {}
    if duration_seconds > 30.0:
        target = min(1.0, 30.0 / duration_seconds)
        ratio, watch = min(points, key=lambda pair: abs(pair[0] - target))
        result["first_30s_retention_pct"] = round(watch * 100.0, 2)
        result["first_30s_ratio_sampled_at"] = round(ratio, 4)

    if len(points) >= 2:
        best_drop = (0.0, 0.0)
        for previous, current in zip(points, points[1:]):
            delta = previous[1] - current[1]
            if delta > best_drop[1]:
                best_drop = (current[0], delta)
        if best_drop[1] > 0.0:
            ratio, delta = best_drop
            zone = "opening" if ratio <= 0.25 else ("middle" if ratio <= 0.75 else "ending")
            result["biggest_drop_zone"] = zone
            result["biggest_drop_pp"] = round(delta * 100.0, 2)
            result["biggest_drop_ratio"] = round(ratio, 4)
        result["end_retention_pct"] = round(points[-1][1] * 100.0, 2)
    return result


def _format_of(video: dict[str, Any]) -> str:
    title = str(video.get("title") or "").casefold()
    if "خارج النص" in title or "بودكاست" in title or "podcast" in title:
        return "podcast"
    duration = _safe_float(video.get("duration_seconds")) or 0.0
    return "short" if 0.0 < duration <= 180.0 else "film"


def _sample(
    access_token: str,
    video: dict[str, Any],
    *,
    now: datetime,
    get_json: Callable[..., dict[str, Any]] = _get_json,
) -> dict[str, Any] | None:
    try:
        metrics = _base_metrics(access_token, video, now=now, get_json=get_json)
    except Exception:
        return None
    if not metrics:
        return None

    duration = _safe_float(video.get("duration_seconds")) or 0.0
    retention: dict[str, object] = {}
    try:
        points = _retention_points(access_token, video, now=now, get_json=get_json)
        retention = _retention_summary(points, duration)
    except Exception:
        retention = {}

    return {
        "video_id": str(video.get("id") or ""),
        "title": " ".join(str(video.get("title") or "").split())[:180],
        "published_at": str(video.get("published_at") or ""),
        "duration_seconds": round(duration, 2),
        "format": _format_of(video),
        "observed_at": _iso(now),
        **{key: round(float(value), 3) for key, value in metrics.items()},
        **retention,
    }


def _median(values: list[object]) -> float | None:
    clean = [value for raw in values if (value := _safe_float(raw)) is not None]
    return round(statistics.median(clean), 2) if clean else None


def _format_insight(format_name: str, samples: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(samples) < MIN_FORMAT_SAMPLES:
        return None

    avg_pct = _median([item.get("avg_percentage_viewed") for item in samples])
    first_30 = _median([item.get("first_30s_retention_pct") for item in samples])
    zones = [
        str(item.get("biggest_drop_zone") or "")
        for item in samples
        if str(item.get("biggest_drop_zone") or "") in {"opening", "middle", "ending"}
    ]
    recurring_zone = None
    recurring_support = 0
    if zones:
        zone, support = Counter(zones).most_common(1)[0]
        if support >= 2 and support / len(samples) >= 0.5:
            recurring_zone = zone
            recurring_support = support

    with_apv = [
        item for item in samples
        if _safe_float(item.get("avg_percentage_viewed")) is not None
    ]
    best = max(
        with_apv,
        key=lambda item: float(item.get("avg_percentage_viewed") or 0.0),
        default=None,
    )
    return {
        "format": format_name,
        "sample_count": len(samples),
        "median_avg_percentage_viewed": avg_pct,
        "median_first_30s_retention_pct": first_30,
        "recurring_largest_drop_zone": recurring_zone,
        "recurring_drop_support": recurring_support,
        "best_recent_video": (
            {
                "video_id": str(best.get("video_id") or ""),
                "title": str(best.get("title") or ""),
                "avg_percentage_viewed": round(float(best.get("avg_percentage_viewed") or 0.0), 2),
            }
            if best is not None
            else None
        ),
    }


def build_insights(samples: list[dict[str, Any]]) -> dict[str, Any]:
    insights: list[dict[str, Any]] = []
    for format_name in ("short", "film", "podcast"):
        group = [item for item in samples if str(item.get("format") or "") == format_name]
        insight = _format_insight(format_name, group)
        if insight is not None:
            insights.append(insight)
    return {
        "mode": "observational_only",
        "causal_claims": False,
        "automatic_production_changes": False,
        "minimum_samples_per_format": MIN_FORMAT_SAMPLES,
        "formats": insights,
    }


def _format_label(value: str) -> str:
    return {"short": "Short", "film": "Film", "podcast": "Podcast"}.get(value, value)


def learning_memo(state: dict[str, Any], kind: str | None = None) -> str:
    root = state.get(STATE_KEY)
    if not isinstance(root, dict):
        return ""
    insights = root.get("insights")
    if not isinstance(insights, dict):
        return ""
    formats = [item for item in (insights.get("formats") or []) if isinstance(item, dict)]
    if kind in {"short", "long"}:
        wanted = {"short"} if kind == "short" else {"film", "podcast"}
        formats = [item for item in formats if str(item.get("format") or "") in wanted]
    if not formats:
        return ""

    lines = [
        "CHANNEL_YOUTUBE_LEARNING — own-channel observational evidence; not causal and never an automatic production override."
    ]
    for item in formats:
        label = _format_label(str(item.get("format") or ""))
        parts = [f"{label}: n={int(item.get('sample_count', 0) or 0)}"]
        apv = _safe_float(item.get("median_avg_percentage_viewed"))
        first_30 = _safe_float(item.get("median_first_30s_retention_pct"))
        if apv is not None:
            parts.append(f"median average viewed={apv:.1f}%")
        if first_30 is not None:
            parts.append(f"median 30s retention={first_30:.1f}%")
        zone = str(item.get("recurring_largest_drop_zone") or "")
        support = int(item.get("recurring_drop_support", 0) or 0)
        if zone:
            parts.append(f"repeated largest-drop zone={zone} ({support}/{int(item.get('sample_count', 0) or 0)})")
        best = item.get("best_recent_video")
        if isinstance(best, dict) and str(best.get("title") or "").strip():
            parts.append(
                f"strongest recent APV example='{str(best.get('title') or '')[:90]}' "
                f"({float(best.get('avg_percentage_viewed', 0.0) or 0.0):.1f}%)"
            )
        lines.append("; ".join(parts) + ".")
    lines.append(
        "Use this only to prioritize structural review (opening/pacing/ending) and preserve patterns worth testing; "
        "do not infer why a metric moved and do not copy a past topic merely because it retained well."
    )
    return "\n".join(lines)[:1700]


def learning_evidence_line(state: dict[str, Any], kind: str) -> str:
    root = state.get(STATE_KEY)
    insights = root.get("insights") if isinstance(root, dict) else None
    formats = [item for item in ((insights or {}).get("formats") or []) if isinstance(item, dict)]
    wanted = {"short"} if kind == "short" else {"film", "podcast"}
    selected = [item for item in formats if str(item.get("format") or "") in wanted]
    if not selected:
        return ""
    item = max(selected, key=lambda row: int(row.get("sample_count", 0) or 0))
    label = _format_label(str(item.get("format") or ""))
    n = int(item.get("sample_count", 0) or 0)
    parts = [f"[Channel learning] {label} n={n}"]
    apv = _safe_float(item.get("median_avg_percentage_viewed"))
    first_30 = _safe_float(item.get("median_first_30s_retention_pct"))
    if apv is not None:
        parts.append(f"median viewed {apv:.1f}%")
    if first_30 is not None:
        parts.append(f"30s {first_30:.1f}%")
    zone = str(item.get("recurring_largest_drop_zone") or "")
    support = int(item.get("recurring_drop_support", 0) or 0)
    if zone:
        parts.append(f"repeat drop {zone} {support}/{n}")
    return "; ".join(parts)[:240]


def _is_due(root: dict[str, Any], now: datetime) -> bool:
    last = _parse_time(root.get("last_attempt_at"))
    if last is None:
        return True
    status = str(root.get("status") or "")
    hours = ERROR_RETRY_HOURS if status in {"error", "unavailable"} else REFRESH_HOURS
    return now - last >= timedelta(hours=hours)


def refresh_state(
    state: dict[str, Any],
    *,
    youtube_api_key: str,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    now: datetime | None = None,
    fetch_live: Callable[[str], dict[str, Any]] = telegram_youtube_stats.fetch_live,
    post_form: Callable[..., dict[str, Any]] = _post_form,
    get_json: Callable[..., dict[str, Any]] = _get_json,
) -> dict[str, Any]:
    current = (now or _now()).astimezone(timezone.utc)
    existing = state.get(STATE_KEY)
    root = dict(existing) if isinstance(existing, dict) else {}
    if not _is_due(root, current):
        return {"status": "skipped_fresh", "changed": False, "state": state}

    root["last_attempt_at"] = _iso(current)
    credentials = {
        "YOUTUBE_API_KEY": str(youtube_api_key or "").strip(),
        "YOUTUBE_CLIENT_ID": str(client_id or "").strip(),
        "YOUTUBE_CLIENT_SECRET": str(client_secret or "").strip(),
        "YOUTUBE_REFRESH_TOKEN": str(refresh_token or "").strip(),
    }
    missing = [name for name, value in credentials.items() if not value]
    if missing:
        root.update(
            {
                "status": "unavailable",
                "reason": "missing_credentials",
                "missing": missing,
            }
        )
        state[STATE_KEY] = root
        return {"status": "unavailable", "changed": True, "state": state}

    try:
        access_token = _refresh_access_token(
            credentials["YOUTUBE_CLIENT_ID"],
            credentials["YOUTUBE_CLIENT_SECRET"],
            credentials["YOUTUBE_REFRESH_TOKEN"],
            post_form=post_form,
        )
        live = fetch_live(credentials["YOUTUBE_API_KEY"])
        videos = [item for item in (live.get("videos") or []) if isinstance(item, dict)][:MAX_VIDEOS]
        fresh_samples: list[dict[str, Any]] = []
        for video in videos:
            sample = _sample(access_token, video, now=current, get_json=get_json)
            if sample is not None and sample.get("video_id"):
                fresh_samples.append(sample)

        previous = [
            item for item in (root.get("samples") or [])
            if isinstance(item, dict) and str(item.get("video_id") or "")
        ]
        by_id = {str(item.get("video_id")): item for item in previous}
        for item in fresh_samples:
            by_id[str(item.get("video_id"))] = item
        samples = sorted(
            by_id.values(),
            key=lambda item: str(item.get("published_at") or ""),
            reverse=True,
        )[:MAX_SAMPLES]
        root.update(
            {
                "schema_version": 1,
                "status": "success",
                "reason": None,
                "missing": [],
                "last_success_at": _iso(current),
                "channel_id": str(live.get("channel_id") or ""),
                "channel_title": str(live.get("channel_title") or "")[:120],
                "samples": samples,
                "insights": build_insights(samples),
            }
        )
        state[STATE_KEY] = root
        return {
            "status": "success",
            "changed": True,
            "sample_count": len(samples),
            "fresh_sample_count": len(fresh_samples),
            "state": state,
        }
    except Exception as exc:
        root.update(
            {
                "status": "error",
                "reason": type(exc).__name__,
            }
        )
        state[STATE_KEY] = root
        return {"status": "error", "changed": True, "state": state}


def refresh_state_file(path: Path) -> dict[str, Any]:
    from scripts.telegram_control_panel import load_state, save_state

    state_path = Path(path)
    state = load_state(state_path)
    result = refresh_state(
        state,
        youtube_api_key=os.environ.get("YOUTUBE_API_KEY", ""),
        client_id=os.environ.get("YOUTUBE_CLIENT_ID", ""),
        client_secret=os.environ.get("YOUTUBE_CLIENT_SECRET", ""),
        refresh_token=os.environ.get("YOUTUBE_REFRESH_TOKEN", ""),
    )
    if result.get("changed"):
        save_state(state_path, state)
    return {key: value for key, value in result.items() if key != "state"}


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    refresh = sub.add_parser("refresh")
    refresh.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "refresh":
        result = refresh_state_file(args.state)
        print(
            "YouTube learning lite: "
            f"status={result.get('status')} "
            f"samples={int(result.get('sample_count', 0) or 0)} "
            f"fresh={int(result.get('fresh_sample_count', 0) or 0)}"
        )


if __name__ == "__main__":
    main()
