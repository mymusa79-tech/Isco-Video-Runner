from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

SCHEMA_VERSION = 1
DEFAULT_MIN_EVIDENCE = 3


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_text(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} must be non-empty")
    return text


def _normalize_refs(values: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    refs: list[str] = []
    for value in values or ():
        text = str(value or "").strip()
        if text and text not in refs:
            refs.append(text)
    return tuple(refs)


class AutonomyMode(str, Enum):
    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AUTOPILOT = "autopilot"


@dataclass(frozen=True)
class StablePreference:
    key: str
    value: Any
    scope: str
    provenance: str
    evidence_refs: tuple[str, ...]
    created_at: str
    updated_at: str
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class EpisodicDecision:
    event_id: str
    video_id: str
    decision_type: str
    options_presented: tuple[str, ...]
    selection: str
    reason: str
    packet_version: str
    outcome_ref: str
    occurred_at: str
    metadata: dict[str, Any]
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class OperationalPolicy:
    autonomy_mode: str
    auto_retry_enabled: bool
    require_publish_approval: bool
    updated_at: str
    updated_by: str = "user"
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class LearnedPreference:
    preference_id: str
    hypothesis: str
    scope: str
    confidence: float
    evidence_count: int
    recency: str
    evidence_refs: tuple[str, ...]
    explanation: str
    created_at: str
    last_updated_at: str
    status: str = "active"
    schema_version: int = SCHEMA_VERSION

    def explain(self) -> dict[str, Any]:
        return {
            "hypothesis": self.hypothesis,
            "scope": self.scope,
            "confidence": self.confidence,
            "evidence_count": self.evidence_count,
            "recency": self.recency,
            "evidence_refs": list(self.evidence_refs),
            "explanation": self.explanation,
            "status": self.status,
        }


@dataclass(frozen=True)
class LiveState:
    video_id: str
    status: str
    stage: str
    run_id: str
    source: str
    observed_at: str
    available: bool = True
    reason: str = ""

    @classmethod
    def unavailable(cls, video_id: str, reason: str) -> "LiveState":
        return cls(
            video_id=_require_text(video_id, "video_id"),
            status="unknown",
            stage="",
            run_id="",
            source="live-source-unavailable",
            observed_at=_utcnow(),
            available=False,
            reason=str(reason or "unavailable"),
        )


class LiveStateProvider(Protocol):
    def fetch(self, video_id: str) -> LiveState: ...


class ChannelOSMemory:
    """Five-layer Channel OS memory boundary.

    Persistent files exist only for stable preferences, episodic decisions,
    operational policies and learned preferences. Live state has no persistence
    path by design and is always fetched through a live provider.
    """

    STABLE_FILE = "stable_preferences.json"
    EPISODIC_FILE = "episodic_decisions.jsonl"
    POLICY_FILE = "operational_policies.json"
    LEARNED_FILE = "learned_preferences.json"

    def __init__(self, root: str | Path, *, min_evidence: int = DEFAULT_MIN_EVIDENCE) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.min_evidence = max(2, int(min_evidence))

    @property
    def persistent_paths(self) -> dict[str, Path]:
        return {
            "stable_preferences": self.root / self.STABLE_FILE,
            "episodic_decisions": self.root / self.EPISODIC_FILE,
            "operational_policies": self.root / self.POLICY_FILE,
            "learned_preferences": self.root / self.LEARNED_FILE,
        }

    def _load_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _atomic_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def stable_preferences(self) -> list[StablePreference]:
        raw = self._load_json(self.persistent_paths["stable_preferences"], [])
        return [
            StablePreference(**{**item, "evidence_refs": tuple(item.get("evidence_refs", []))})
            for item in raw
        ]

    def set_stable_preference(
        self,
        *,
        key: str,
        value: Any,
        scope: str = "channel",
        explicit_user_statement: bool = False,
        evidence_refs: list[str] | tuple[str, ...] | None = None,
    ) -> StablePreference:
        key = _require_text(key, "key")
        scope = _require_text(scope, "scope")
        refs = _normalize_refs(evidence_refs)
        if not explicit_user_statement and len(refs) < self.min_evidence:
            raise ValueError(
                f"stable preference requires explicit user declaration or at least {self.min_evidence} independent evidence refs"
            )
        provenance = "explicit_user_statement" if explicit_user_statement else "validated_repeated_evidence"
        now = _utcnow()
        items = self.stable_preferences()
        previous = next((item for item in items if item.key == key and item.scope == scope), None)
        record = StablePreference(
            key=key,
            value=value,
            scope=scope,
            provenance=provenance,
            evidence_refs=refs,
            created_at=previous.created_at if previous else now,
            updated_at=now,
        )
        items = [item for item in items if not (item.key == key and item.scope == scope)] + [record]
        self._atomic_json(
            self.persistent_paths["stable_preferences"],
            [{**asdict(item), "evidence_refs": list(item.evidence_refs)} for item in items],
        )
        return record

    def append_episode(
        self,
        *,
        event_id: str,
        video_id: str,
        decision_type: str,
        options_presented: list[str] | tuple[str, ...] = (),
        selection: str = "",
        reason: str = "",
        packet_version: str = "",
        outcome_ref: str = "",
        occurred_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> EpisodicDecision:
        record = EpisodicDecision(
            event_id=_require_text(event_id, "event_id"),
            video_id=str(video_id or "").strip(),
            decision_type=_require_text(decision_type, "decision_type"),
            options_presented=tuple(str(x) for x in options_presented),
            selection=str(selection or ""),
            reason=str(reason or ""),
            packet_version=str(packet_version or ""),
            outcome_ref=str(outcome_ref or ""),
            occurred_at=occurred_at or _utcnow(),
            metadata=dict(metadata or {}),
        )
        path = self.persistent_paths["episodic_decisions"]
        with path.open("a", encoding="utf-8") as fh:
            payload = asdict(record)
            payload["options_presented"] = list(record.options_presented)
            fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return record

    def episodes(self) -> list[EpisodicDecision]:
        path = self.persistent_paths["episodic_decisions"]
        if not path.exists():
            return []
        records: list[EpisodicDecision] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                item = json.loads(line)
                item["options_presented"] = tuple(item.get("options_presented", []))
                records.append(EpisodicDecision(**item))
        return records

    def get_policy(self) -> OperationalPolicy:
        path = self.persistent_paths["operational_policies"]
        raw = self._load_json(path, None)
        if raw is None:
            return OperationalPolicy(
                autonomy_mode=AutonomyMode.BALANCED.value,
                auto_retry_enabled=True,
                require_publish_approval=True,
                updated_at="",
            )
        return OperationalPolicy(**raw)

    def set_policy(
        self,
        *,
        autonomy_mode: str | AutonomyMode,
        auto_retry_enabled: bool,
        require_publish_approval: bool = True,
        explicit_user_change: bool = False,
    ) -> OperationalPolicy:
        if not explicit_user_change:
            raise ValueError("operational policies may only change through an explicit user action")
        mode = AutonomyMode(autonomy_mode).value
        if require_publish_approval is not True:
            raise ValueError("REQUIRE_PUBLISH_APPROVAL is an immutable Channel OS firewall")
        policy = OperationalPolicy(
            autonomy_mode=mode,
            auto_retry_enabled=bool(auto_retry_enabled),
            require_publish_approval=True,
            updated_at=_utcnow(),
        )
        self._atomic_json(self.persistent_paths["operational_policies"], asdict(policy))
        return policy

    def learned_preferences(self) -> list[LearnedPreference]:
        raw = self._load_json(self.persistent_paths["learned_preferences"], [])
        return [
            LearnedPreference(**{**item, "evidence_refs": tuple(item.get("evidence_refs", []))})
            for item in raw
        ]

    def upsert_learned_preference(
        self,
        *,
        preference_id: str,
        hypothesis: str,
        scope: str,
        confidence: float,
        evidence_refs: list[str] | tuple[str, ...],
        explanation: str,
        recency: str | None = None,
    ) -> LearnedPreference:
        preference_id = _require_text(preference_id, "preference_id")
        refs = _normalize_refs(evidence_refs)
        if not refs:
            raise ValueError("learned preference requires at least one evidence reference")
        confidence = float(confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        existing_items = self.learned_preferences()
        previous = next((item for item in existing_items if item.preference_id == preference_id), None)
        now = _utcnow()
        record = LearnedPreference(
            preference_id=preference_id,
            hypothesis=_require_text(hypothesis, "hypothesis"),
            scope=_require_text(scope, "scope"),
            confidence=confidence,
            evidence_count=len(refs),
            recency=recency or now,
            evidence_refs=refs,
            explanation=_require_text(explanation, "explanation"),
            created_at=previous.created_at if previous else now,
            last_updated_at=now,
            status=previous.status if previous and previous.status == "do_not_promote" else "active",
        )
        items = [item for item in existing_items if item.preference_id != preference_id] + [record]
        self._atomic_json(
            self.persistent_paths["learned_preferences"],
            [{**asdict(item), "evidence_refs": list(item.evidence_refs)} for item in items],
        )
        return record

    def mark_do_not_promote(self, *, preference_id: str, event_id: str, reason: str = "user correction") -> LearnedPreference:
        items = self.learned_preferences()
        previous = next((item for item in items if item.preference_id == preference_id), None)
        if previous is None:
            raise KeyError(preference_id)
        updated = LearnedPreference(**{**asdict(previous), "status": "do_not_promote"})
        items = [item for item in items if item.preference_id != preference_id] + [updated]
        self._atomic_json(
            self.persistent_paths["learned_preferences"],
            [{**asdict(item), "evidence_refs": list(item.evidence_refs)} for item in items],
        )
        self.append_episode(
            event_id=event_id,
            video_id="",
            decision_type="learned_preference_correction",
            selection="do_not_promote",
            reason=reason,
            metadata={"preference_id": preference_id},
        )
        return updated

    def promote_learned_preference(self, *, preference_id: str, key: str, value: Any) -> StablePreference:
        learned = next((item for item in self.learned_preferences() if item.preference_id == preference_id), None)
        if learned is None:
            raise KeyError(preference_id)
        if learned.status != "active":
            raise ValueError("learned preference is blocked from promotion")
        if learned.evidence_count < self.min_evidence:
            raise ValueError("single/insufficient evidence cannot become a stable preference")
        return self.set_stable_preference(
            key=key,
            value=value,
            scope=learned.scope,
            explicit_user_statement=False,
            evidence_refs=learned.evidence_refs,
        )

    def read_live_state(self, provider: LiveStateProvider, video_id: str) -> LiveState:
        video_id = _require_text(video_id, "video_id")
        try:
            state = provider.fetch(video_id)
        except Exception as exc:
            return LiveState.unavailable(video_id, str(exc))
        if not isinstance(state, LiveState):
            raise TypeError("live state provider must return LiveState")
        if state.video_id != video_id:
            raise ValueError("live state provider returned a different video identity")
        return state
