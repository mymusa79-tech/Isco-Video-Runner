from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable

from scripts.channel_brain import BrainAnalysis
from scripts.channel_os_memory import LearnedPreference
from scripts.channel_os_mission_control import MissionSnapshot
from scripts.channel_os_trust_engine import TrustDecision

OPPORTUNITY_MIN_CONFIDENCE = 0.70
OPPORTUNITY_MIN_EVIDENCE = 3
DEFAULT_MAX_OPPORTUNITIES_24H = 2
DEFAULT_MAX_DIGESTS_24H = 1


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _text(value: object, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{field} must be non-empty")
    return result


def _fingerprint(*parts: object) -> str:
    raw = "\x1f".join(str(part or "").strip() for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class InterventionLevel(str, Enum):
    INTERRUPT_NOW = "interrupt_now"
    OPPORTUNITY = "opportunity"
    DIGEST = "digest"


@dataclass(frozen=True)
class NotificationBudget:
    max_opportunities_24h: int = DEFAULT_MAX_OPPORTUNITIES_24H
    max_digests_24h: int = DEFAULT_MAX_DIGESTS_24H

    def __post_init__(self) -> None:
        if self.max_opportunities_24h < 0 or self.max_digests_24h < 0:
            raise ValueError("notification budget limits must be non-negative")


@dataclass(frozen=True)
class ProactiveSignal:
    signal_id: str
    level: str
    title: str
    reason: str
    evidence: tuple[str, ...]
    action_label: str
    action_callback: str
    confidence: float
    video_id: str = ""
    source: str = ""
    state_token: str = ""
    authority: str = "advisory_only"

    def __post_init__(self) -> None:
        _text(self.signal_id, "signal_id")
        InterventionLevel(self.level)
        _text(self.title, "title")
        _text(self.reason, "reason")
        if not self.evidence:
            raise ValueError("proactive signals require explicit evidence")
        if not self.action_label or not self.action_callback:
            raise ValueError("proactive signals require one concrete action")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if self.authority != "advisory_only":
            raise ValueError("Proactive Operator V1 is advisory-only")
        callback = self.action_callback.encode("utf-8")
        if len(callback) > 64:
            raise ValueError("Telegram callback exceeds 64-byte limit")
        lowered = self.action_callback.casefold()
        if "publish" in lowered or "upload" in lowered:
            raise ValueError("Proactive Operator may not create a publish action")

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.signal_id, self.level, self.video_id, self.state_token, self.reason, self.action_callback)


@dataclass(frozen=True)
class DeliveryRecord:
    fingerprint: str
    level: str
    sent_at: str


@dataclass(frozen=True)
class DeliveryDecision:
    signal: ProactiveSignal
    deliver: bool
    suppression_reason: str = ""


class ProactiveDeliveryLedger:
    """Delivery accounting only; never a source for Live State or user preferences."""

    FILE_NAME = "proactive_delivery_ledger.json"

    def __init__(self, root: str | Path) -> None:
        self.path = Path(root) / self.FILE_NAME
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> list[DeliveryRecord]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise RuntimeError("proactive delivery ledger is malformed")
        return [DeliveryRecord(**item) for item in raw]

    def _write(self, records: list[DeliveryRecord]) -> None:
        fd, temp = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump([asdict(item) for item in records], handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def recent(self, *, now: datetime | None = None, hours: int = 24) -> tuple[DeliveryRecord, ...]:
        now = (now or _utcnow()).astimezone(timezone.utc)
        cutoff = now - timedelta(hours=hours)
        return tuple(item for item in self._read() if _parse(item.sent_at) >= cutoff)

    def record(self, signal: ProactiveSignal, *, sent_at: datetime | None = None) -> DeliveryRecord:
        record = DeliveryRecord(signal.fingerprint, signal.level, _iso(sent_at or _utcnow()))
        records = self._read()
        records.append(record)
        self._write(records)
        return record


class ProactiveOperator:
    """Evidence-gated advisory planner. It never sends, retries, schedules or publishes itself."""

    def __init__(self, ledger: ProactiveDeliveryLedger, *, budget: NotificationBudget | None = None) -> None:
        self.ledger = ledger
        self.budget = budget or NotificationBudget()

    @staticmethod
    def _callback(prefix: str, identity: str) -> str:
        token = hashlib.sha256(str(identity).encode("utf-8")).hexdigest()[:16]
        return f"cmd:channelos-{prefix}:{token}"

    def interrupt_signals(
        self,
        snapshot: MissionSnapshot,
        trust_decisions: Iterable[TrustDecision] = (),
    ) -> tuple[ProactiveSignal, ...]:
        signals: list[ProactiveSignal] = []
        for item in snapshot.items:
            if item.mission_state != "Needs Me":
                continue
            reason = item.reason or "A live production decision is required before safe progress can continue."
            signals.append(
                ProactiveSignal(
                    signal_id=f"needs-me:{item.video_id}:{item.run_id}",
                    level=InterventionLevel.INTERRUPT_NOW.value,
                    title=f"قرار مطلوب — {item.title or item.video_id}",
                    reason=reason,
                    evidence=(f"live_state={item.mission_state}", f"source={item.source}", f"run_id={item.run_id}"),
                    action_label="عرض القرار",
                    action_callback=self._callback("needs", f"{item.video_id}:{item.run_id}"),
                    confidence=1.0,
                    video_id=item.video_id,
                    source="mission-control-live-state",
                    state_token=f"{item.mission_state}:{item.run_id}:{item.reason}",
                )
            )
        for decision in trust_decisions:
            if decision.failure_class not in {"Needs Choice", "Unsafe"}:
                continue
            unsafe = decision.failure_class == "Unsafe"
            signals.append(
                ProactiveSignal(
                    signal_id=f"trust:{decision.failure_id}:{decision.failure_class}",
                    level=InterventionLevel.INTERRUPT_NOW.value,
                    title="توقف آمن مطلوب" if unsafe else "اختيار مطلوب",
                    reason=decision.reason,
                    evidence=(f"failure_class={decision.failure_class}", f"trust_action={decision.action}", "publish_approval=required"),
                    action_label="عرض التفاصيل",
                    action_callback=self._callback("failure", decision.failure_id),
                    confidence=1.0,
                    source="trust-engine",
                    state_token=f"{decision.failure_class}:{decision.action}:{decision.reason}",
                )
            )
        # Same material state produces one interruption even if multiple input paths describe it.
        deduped: dict[str, ProactiveSignal] = {}
        for signal in signals:
            deduped.setdefault(signal.fingerprint, signal)
        return tuple(deduped.values())

    def opportunity_from_brain(self, analysis: BrainAnalysis) -> ProactiveSignal | None:
        if analysis.authority != "advisory_only" or not analysis.what_should_change_next_time:
            return None
        comparable = [comparison for comparison in analysis.comparisons if comparison.status == "comparable"]
        if not comparable:
            return None
        strongest = max(comparable, key=lambda item: (item.confidence, item.evidence_count))
        if strongest.confidence < OPPORTUNITY_MIN_CONFIDENCE or strongest.evidence_count < OPPORTUNITY_MIN_EVIDENCE:
            return None
        recommendation = analysis.what_should_change_next_time[0]
        if recommendation.startswith("Insufficient reliable signal"):
            return None
        evidence = list(analysis.what_lost or analysis.what_won)
        evidence.append(
            f"strongest_cohort={strongest.cohort}; evidence_count={strongest.evidence_count}; confidence={strongest.confidence:.2f}"
        )
        return ProactiveSignal(
            signal_id=f"brain:{analysis.video_id}:{analysis.logic_version}",
            level=InterventionLevel.OPPORTUNITY.value,
            title="فرصة مدعومة بأداء القناة",
            reason=recommendation,
            evidence=tuple(evidence),
            action_label="مراجعة التوصية",
            action_callback=self._callback("brain", analysis.video_id),
            confidence=strongest.confidence,
            video_id=analysis.video_id,
            source="channel-brain",
            state_token=_fingerprint(analysis.logic_version, *analysis.what_won, *analysis.what_lost, *analysis.what_should_change_next_time),
        )

    def opportunity_from_learned_preference(self, preference: LearnedPreference) -> ProactiveSignal | None:
        if preference.status != "active":
            return None
        if preference.confidence < OPPORTUNITY_MIN_CONFIDENCE or preference.evidence_count < OPPORTUNITY_MIN_EVIDENCE:
            return None
        return ProactiveSignal(
            signal_id=f"learned:{preference.preference_id}",
            level=InterventionLevel.OPPORTUNITY.value,
            title="نمط تفضيل يستحق المراجعة",
            reason=preference.explanation,
            evidence=tuple(preference.evidence_refs) + (
                f"evidence_count={preference.evidence_count}; confidence={preference.confidence:.2f}; recency={preference.recency}",
            ),
            action_label="راجع لماذا",
            action_callback=self._callback("preference", preference.preference_id),
            confidence=preference.confidence,
            source="learned-preference-advisory",
            state_token=_fingerprint(preference.last_updated_at, preference.status, preference.confidence, *preference.evidence_refs),
        )

    def digest_signal(
        self,
        snapshot: MissionSnapshot,
        *,
        brain_analyses: Iterable[BrainAnalysis] = (),
    ) -> ProactiveSignal | None:
        lines: list[str] = []
        problem_items = [item for item in snapshot.items if item.mission_state == "Problems"]
        if problem_items:
            lines.append(f"{len(problem_items)} item(s) currently in Problems; inspect when convenient unless Trust Engine escalates one.")
        weak_analyses = []
        for analysis in brain_analyses:
            opportunity = self.opportunity_from_brain(analysis)
            if opportunity is None and analysis.what_should_change_next_time:
                weak_analyses.append(analysis)
        if weak_analyses:
            lines.append(f"{len(weak_analyses)} analytics observation(s) are below the opportunity threshold and remain digest-only.")
        if not lines:
            return None
        state_token = _fingerprint(snapshot.observed_at, *lines)
        return ProactiveSignal(
            signal_id=f"digest:{state_token[:16]}",
            level=InterventionLevel.DIGEST.value,
            title="ملخص Channel OS",
            reason=" ".join(lines),
            evidence=(f"mission_snapshot_observed_at={snapshot.observed_at}", "low-urgency information only"),
            action_label="فتح Mission Control",
            action_callback="cmd:channelos-refresh",
            confidence=1.0,
            source="channel-os-digest",
            state_token=state_token,
        )

    def delivery_decision(self, signal: ProactiveSignal, *, now: datetime | None = None) -> DeliveryDecision:
        now = (now or _utcnow()).astimezone(timezone.utc)
        recent = self.ledger.recent(now=now, hours=24)
        if signal.level == InterventionLevel.INTERRUPT_NOW.value:
            if any(item.fingerprint == signal.fingerprint for item in recent):
                return DeliveryDecision(signal, False, "duplicate_interrupt_without_material_state_change")
            return DeliveryDecision(signal, True)
        if signal.level == InterventionLevel.OPPORTUNITY.value:
            if signal.confidence < OPPORTUNITY_MIN_CONFIDENCE or len(signal.evidence) < 1:
                return DeliveryDecision(signal, False, "opportunity_evidence_below_threshold")
            sent = sum(item.level == InterventionLevel.OPPORTUNITY.value for item in recent)
            if sent >= self.budget.max_opportunities_24h:
                return DeliveryDecision(signal, False, "opportunity_notification_budget_exhausted")
            if any(item.fingerprint == signal.fingerprint for item in recent):
                return DeliveryDecision(signal, False, "duplicate_opportunity")
            return DeliveryDecision(signal, True)
        sent = sum(item.level == InterventionLevel.DIGEST.value for item in recent)
        if sent >= self.budget.max_digests_24h:
            return DeliveryDecision(signal, False, "digest_notification_budget_exhausted")
        if any(item.fingerprint == signal.fingerprint for item in recent):
            return DeliveryDecision(signal, False, "duplicate_digest")
        return DeliveryDecision(signal, True)


def render_telegram(signal: ProactiveSignal) -> tuple[str, list[list[dict[str, str]]]]:
    icon = {
        InterventionLevel.INTERRUPT_NOW.value: "🚨",
        InterventionLevel.OPPORTUNITY.value: "💡",
        InterventionLevel.DIGEST.value: "📌",
    }[signal.level]
    lines = [f"{icon} {signal.title}", "", f"السبب: {signal.reason}", "", "الدليل:"]
    lines.extend(f"• {item}" for item in signal.evidence[:6])
    lines.extend(["", f"Confidence: {signal.confidence:.2f}", "Authority: advisory only"])
    keyboard = [[{"text": signal.action_label, "callback_data": signal.action_callback}]]
    return "\n".join(lines), keyboard
