"""Bounded live-market reliability for Telegram Topic Research V2.

Three live candidates are the preferred UX target, not an all-or-nothing validity
condition. The adapter probes incrementally: five candidates first, then two at a
time, up to ten. It stops early when three measured candidates exist. If the pool
is exhausted with one or two measured candidates, those valid candidates are
returned; zero measured candidates is the only live-market shortfall.

No fallback or unmeasured candidate is ever promoted to live evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

TARGET_LIVE_MARKET_CANDIDATES = 3
MIN_LIVE_MARKET_CANDIDATES = 1
INITIAL_MARKET_PROBE_BATCH = 5
MARKET_PROBE_EXPANSION_BATCH = 2
MAX_MARKET_PROBE_CANDIDATES = 10


@dataclass(frozen=True)
class LiveMarketEvidenceShortfall(RuntimeError):
    verified: int
    required: int
    probes_used: int
    probe_limit: int

    def __str__(self) -> str:
        return (
            f"Live market evidence produced only {self.verified} candidates; "
            f"need at least {self.required}; probes_used={self.probes_used}; "
            f"probe_limit={self.probe_limit}"
        )


def _is_live_measured(candidate: Any) -> bool:
    return (
        str(getattr(candidate, "market_timing_status", "") or "") == "measured"
        and str(getattr(candidate, "source_mode", "") or "") == "live_research"
    )


def _candidate_identity(candidate: Any) -> str:
    query = str(getattr(candidate, "market_query", "") or "").strip().casefold()
    title = str(getattr(candidate, "title", "") or "").strip().casefold()
    return query or title


def adaptive_measure_market_timing(
    original: Callable[..., list[Any]],
    youtube_key: str | None,
    candidates: Iterable[Any],
    *,
    region: str = "SA",
    language: str = "ar",
    target_live: int = TARGET_LIVE_MARKET_CANDIDATES,
    minimum_live: int = MIN_LIVE_MARKET_CANDIDATES,
    initial_batch: int = INITIAL_MARKET_PROBE_BATCH,
    expansion_batch: int = MARKET_PROBE_EXPANSION_BATCH,
    hard_cap: int = MAX_MARKET_PROBE_CANDIDATES,
    **kwargs: Any,
) -> list[Any]:
    """Probe toward three results, but preserve any valid partial result."""
    pool = list(candidates)[: max(0, int(hard_cap))]
    minimum = max(1, int(minimum_live))
    target = max(minimum, int(target_live))
    if not pool:
        raise LiveMarketEvidenceShortfall(0, minimum, 0, max(0, int(hard_cap)))

    first = max(1, int(initial_batch))
    step = max(1, int(expansion_batch))
    measured: list[Any] = []
    live_by_identity: dict[str, Any] = {}
    cursor = 0

    while cursor < len(pool) and len(live_by_identity) < target:
        size = first if cursor == 0 else step
        batch = pool[cursor : min(len(pool), cursor + size)]
        if not batch:
            break
        batch_result = original(
            youtube_key,
            batch,
            region=region,
            language=language,
            **kwargs,
        )
        measured.extend(batch_result)
        cursor += len(batch)
        for candidate in batch_result:
            if not _is_live_measured(candidate):
                continue
            identity = _candidate_identity(candidate)
            if identity:
                live_by_identity.setdefault(identity, candidate)

    if len(live_by_identity) < minimum:
        raise LiveMarketEvidenceShortfall(
            len(live_by_identity),
            minimum,
            cursor,
            max(0, int(hard_cap)),
        )
    return measured


def install_market_probe_reliability(engine_research: Any) -> None:
    """Install the adaptive wrapper exactly once on the Engine research module."""
    current = engine_research.measure_market_timing
    if getattr(current, "_isco_adaptive_market_probe", False):
        return

    def wrapped(
        youtube_key: str | None,
        candidates: Iterable[Any],
        *,
        region: str = "SA",
        language: str = "ar",
        **kwargs: Any,
    ) -> list[Any]:
        return adaptive_measure_market_timing(
            current,
            youtube_key,
            candidates,
            region=region,
            language=language,
            **kwargs,
        )

    setattr(wrapped, "_isco_adaptive_market_probe", True)
    setattr(wrapped, "_isco_original_measure_market_timing", current)
    engine_research.measure_market_timing = wrapped


def install_shortfall_reason(core: Any) -> None:
    """Expose verified/required progress to Telegram without inventing market data."""
    current = core._research_failure_reason
    if getattr(current, "_isco_live_shortfall_reason", False):
        return

    def reason(exc: Exception) -> str:
        if isinstance(exc, LiveMarketEvidenceShortfall):
            return (
                f"التقدم: لم توجد أي فرصة تجتاز دليل السوق الحي بعد فحص "
                f"{exc.probes_used} مرشحًا. سيبقى الطلب للمحاولة التلقائية؛ "
                "لن أعرض رقم اهتمام حالي غير موثق."
            )
        return current(exc)

    setattr(reason, "_isco_live_shortfall_reason", True)
    core._research_failure_reason = reason
