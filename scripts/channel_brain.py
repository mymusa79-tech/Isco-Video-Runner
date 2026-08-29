from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from statistics import mean
from typing import Iterable

LOGIC_VERSION = "channel-brain-v1"
MIN_COHORT_SIZE = 3
RECENT_N = 10
SIMILAR_DURATION_RATIO = 0.20
PUBLISH_WINDOW_HOURS = 1
METRICS = ("views", "ctr", "retention")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _topic(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _hour_distance(a: int, b: int) -> int:
    raw = abs(a - b) % 24
    return min(raw, 24 - raw)


@dataclass(frozen=True)
class VideoPerformance:
    video_id: str
    content_type: str
    topic_family: str
    duration_seconds: float
    published_at: str
    views: float | None = None
    ctr: float | None = None
    retention: float | None = None

    def __post_init__(self) -> None:
        if not str(self.video_id).strip():
            raise ValueError("video_id must be non-empty")
        if self.content_type not in {"long", "short"}:
            raise ValueError("content_type must be long or short")
        if float(self.duration_seconds) <= 0:
            raise ValueError("duration_seconds must be positive")
        _parse_time(self.published_at)
        for name in ("ctr", "retention"):
            value = getattr(self, name)
            if value is not None and not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"{name} must be a 0..1 ratio")
        if self.views is not None and float(self.views) < 0:
            raise ValueError("views must be non-negative")


@dataclass(frozen=True)
class MetricComparison:
    metric: str
    target_value: float | None
    baseline_value: float | None
    delta_percent: float | None
    evidence_count: int
    status: str


@dataclass(frozen=True)
class CohortComparison:
    cohort: str
    evidence_ids: tuple[str, ...]
    evidence_count: int
    confidence: float
    status: str
    metrics: tuple[MetricComparison, ...]


@dataclass(frozen=True)
class BrainAnalysis:
    video_id: str
    content_type: str
    logic_version: str
    comparisons: tuple[CohortComparison, ...]
    what_won: tuple[str, ...]
    what_lost: tuple[str, ...]
    what_should_change_next_time: tuple[str, ...]
    missing_metrics: tuple[str, ...]
    authority: str = "advisory_only"

    def as_context(self) -> dict:
        payload = asdict(self)
        payload["authority"] = "advisory_only"
        payload["may_auto_override"] = False
        return payload


class ChannelBrain:
    def __init__(self, *, min_cohort_size: int = MIN_COHORT_SIZE, recent_n: int = RECENT_N) -> None:
        self.min_cohort_size = max(2, int(min_cohort_size))
        self.recent_n = max(self.min_cohort_size, int(recent_n))

    def _history(self, target: VideoPerformance, history: Iterable[VideoPerformance]) -> list[VideoPerformance]:
        items = [item for item in history if item.video_id != target.video_id]
        # Type-safe by default: every baseline that can drive a recommendation is same-type.
        return [item for item in items if item.content_type == target.content_type]

    def _cohorts(self, target: VideoPerformance, history: Iterable[VideoPerformance]) -> dict[str, list[VideoPerformance]]:
        safe = self._history(target, history)
        recent = sorted(safe, key=lambda x: _parse_time(x.published_at), reverse=True)[: self.recent_n]
        target_topic = _topic(target.topic_family)
        same_topic = [x for x in safe if target_topic and _topic(x.topic_family) == target_topic]
        tolerance = target.duration_seconds * SIMILAR_DURATION_RATIO
        similar_duration = [x for x in safe if abs(x.duration_seconds - target.duration_seconds) <= tolerance]
        target_hour = _parse_time(target.published_at).hour
        same_window = [x for x in safe if _hour_distance(_parse_time(x.published_at).hour, target_hour) <= PUBLISH_WINDOW_HOURS]
        return {
            "channel_average": list(safe),
            "recent_n": recent,
            "same_topic": same_topic,
            "same_type": list(safe),
            "similar_duration": similar_duration,
            "same_publish_window": same_window,
        }

    def _comparison(self, name: str, target: VideoPerformance, cohort: list[VideoPerformance]) -> CohortComparison:
        metrics: list[MetricComparison] = []
        for metric in METRICS:
            target_value = getattr(target, metric)
            values = [float(getattr(item, metric)) for item in cohort if getattr(item, metric) is not None]
            evidence_count = len(values)
            baseline = mean(values) if values else None
            if target_value is None:
                status = "target_metric_unavailable"
                delta = None
            elif evidence_count < self.min_cohort_size:
                status = "insufficient_evidence"
                delta = None
            elif baseline is None:
                status = "unavailable"
                delta = None
            elif baseline == 0:
                status = "comparable"
                delta = None
            else:
                delta = (float(target_value) - baseline) / baseline * 100.0
                status = "comparable"
            metrics.append(
                MetricComparison(
                    metric=metric,
                    target_value=None if target_value is None else float(target_value),
                    baseline_value=baseline,
                    delta_percent=delta,
                    evidence_count=evidence_count,
                    status=status,
                )
            )
        metric_evidence = [m.evidence_count for m in metrics if m.target_value is not None]
        effective_count = min(metric_evidence) if metric_evidence else 0
        completeness = sum(m.target_value is not None for m in metrics) / len(METRICS)
        confidence = min(1.0, effective_count / 8.0) * completeness
        status = "comparable" if any(m.status == "comparable" for m in metrics) else "insufficient_evidence"
        return CohortComparison(
            cohort=name,
            evidence_ids=tuple(item.video_id for item in cohort),
            evidence_count=len(cohort),
            confidence=round(confidence, 3),
            status=status,
            metrics=tuple(metrics),
        )

    def analyze(self, target: VideoPerformance, history: Iterable[VideoPerformance]) -> BrainAnalysis:
        cohorts = self._cohorts(target, history)
        comparisons = tuple(self._comparison(name, target, items) for name, items in cohorts.items())
        missing = tuple(metric for metric in METRICS if getattr(target, metric) is None)
        won: list[str] = []
        lost: list[str] = []
        # Avoid duplicate conclusions from equivalent cohorts: choose the strongest available evidence per metric.
        for metric in METRICS:
            candidates = []
            for comparison in comparisons:
                metric_cmp = next(x for x in comparison.metrics if x.metric == metric)
                if metric_cmp.status == "comparable" and metric_cmp.delta_percent is not None:
                    candidates.append((metric_cmp.evidence_count, comparison.cohort, metric_cmp.delta_percent))
            if not candidates:
                continue
            _, cohort_name, delta = max(candidates, key=lambda item: item[0])
            if delta >= 10.0:
                won.append(f"{metric} outperformed {cohort_name} by {delta:.1f}%")
            elif delta <= -10.0:
                lost.append(f"{metric} underperformed {cohort_name} by {abs(delta):.1f}%")

        changes: list[str] = []
        lost_metrics = {entry.split()[0] for entry in lost}
        if "ctr" in lost_metrics:
            changes.append("Test the packaging layer next time (title/thumbnail/hook promise); do not auto-change production.")
        if "retention" in lost_metrics:
            changes.append("Review opening pace and structural retention choices next time; recommendation only.")
        if "views" in lost_metrics and "ctr" not in lost_metrics and "retention" not in lost_metrics:
            changes.append("Treat reach/distribution as the first hypothesis; preserve content choices until stronger evidence exists.")
        if not changes and won:
            changes.append("Preserve the winning characteristics as context for the next editorial decision; do not auto-override it.")
        if not changes:
            changes.append("Insufficient reliable signal for a directional change; collect more comparable samples.")

        return BrainAnalysis(
            video_id=target.video_id,
            content_type=target.content_type,
            logic_version=LOGIC_VERSION,
            comparisons=comparisons,
            what_won=tuple(won),
            what_lost=tuple(lost),
            what_should_change_next_time=tuple(changes),
            missing_metrics=missing,
        )
