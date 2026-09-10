from __future__ import annotations

"""Persistent, context-scoped memory for authoritative visual candidate failures.

This layer never changes provider admission thresholds or retry budgets. It stores only
candidate-local, durable defects in the existing ISCO_HISTORY_PATH document and lets
the shared visual selector skip the same provider-qualified asset when it is reviewed
again in the same narrative context. Transient provider/network failures are never
learned.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
HISTORY_KEY = "visual_failure_memory"
DEFAULT_TTL_DAYS = 30
MAX_ENTRIES = 256

# Reasons that describe the asset/context rather than provider availability.
_CONTEXTUAL_REASONS = {
    "semantic_mismatch",
    "generic_mood",
    "insufficient_semantic_specificity",
    "low_relevance",
    "low_visual_quality",
    "final_cut_not_ready",
}
# Defects that are safe to quarantine irrespective of narrative context.
_GLOBAL_REASONS = {
    "rights_failure",
    "logo_or_watermark",
    "synthetic_or_ai_generated",
    "unsafe_content",
    "corrupt_media",
    "local_preflight_rejection",
}
_TRANSIENT_MARKERS = (
    "timeout",
    "rate_limit",
    "429",
    "network",
    "transport",
    "provider_unavailable",
    "capacity",
    "connection",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _history_path() -> Path | None:
    raw = str(os.environ.get("ISCO_HISTORY_PATH") or "").strip()
    return Path(raw) if raw else None


def _read_history(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return {"videos": []}
    return value if isinstance(value, dict) else {"videos": []}


def _write_history(path: Path, history: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


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


def _normalize_reason(audit: dict[str, Any]) -> str | None:
    status = str(audit.get("status") or "").strip().lower()
    reason_text = " ".join(
        str(audit.get(key) or "").strip().lower()
        for key in ("reason", "error", "failure_reason", "block_reason")
    )
    if any(marker in reason_text for marker in _TRANSIENT_MARKERS):
        return None
    if audit.get("rights_ok") is False or any(token in reason_text for token in ("rights", "license")):
        return "rights_failure"
    if any(token in reason_text for token in ("watermark", "logo")):
        return "logo_or_watermark"
    if any(token in reason_text for token in ("synthetic", "ai-generated", "ai generated")):
        return "synthetic_or_ai_generated"
    if any(token in reason_text for token in ("unsafe", "nsfw", "violence", "sexual")):
        return "unsafe_content"
    if any(token in reason_text for token in ("corrupt", "decode", "invalid media")):
        return "corrupt_media"
    if any(token in reason_text for token in ("preflight", "local quarantine")):
        return "local_preflight_rejection"
    try:
        relevance = float(audit.get("relevance", 1.0) or 0.0)
        quality = float(audit.get("visual_quality", 1.0) or 0.0)
    except (TypeError, ValueError):
        relevance = quality = 1.0
    if relevance < 0.65:
        return "low_relevance"
    if quality < 0.65:
        return "low_visual_quality"
    if status == "pass" and min(relevance, quality) < 0.85:
        return "final_cut_not_ready"
    if "generic" in reason_text or "mood" in reason_text:
        return "generic_mood"
    if "semantic" in reason_text or "relevance" in reason_text:
        return "semantic_mismatch"
    return None


def _pruned(entries: object, *, now: datetime | None = None) -> list[dict[str, Any]]:
    current = now or _now()
    kept: list[dict[str, Any]] = []
    for raw in entries if isinstance(entries, list) else ():
        if not isinstance(raw, dict):
            continue
        expires = _parse_time(raw.get("expires_at"))
        if expires is None or expires <= current:
            continue
        provider = str(raw.get("provider") or "").strip().lower()
        asset_id = str(raw.get("asset_id") or "").strip()
        reason = str(raw.get("reason") or "").strip().lower()
        scope = str(raw.get("scope") or "").strip().lower()
        context = str(raw.get("context_hash") or "").strip()
        if not provider or not asset_id or reason not in (_CONTEXTUAL_REASONS | _GLOBAL_REASONS):
            continue
        if scope not in {"context", "global"}:
            continue
        if scope == "context" and not context:
            continue
        kept.append(dict(raw))
    kept.sort(key=lambda item: str(item.get("recorded_at") or ""), reverse=True)
    return kept[:MAX_ENTRIES]


def should_skip(provider: str, asset_id: object, context_hash: str) -> dict[str, Any] | None:
    path = _history_path()
    if path is None or not path.is_file():
        return None
    history = _read_history(path)
    entries = _pruned(history.get(HISTORY_KEY))
    key_provider = str(provider or "").strip().lower()
    key_asset = str(asset_id).strip()
    key_context = str(context_hash or "").strip()
    for entry in entries:
        if entry.get("provider") != key_provider or str(entry.get("asset_id")) != key_asset:
            continue
        if entry.get("scope") == "global" or entry.get("context_hash") == key_context:
            return entry
    return None


def record_failure(
    provider: str,
    asset_id: object,
    context_hash: str,
    audit: dict[str, Any],
    *,
    ttl_days: int = DEFAULT_TTL_DAYS,
) -> bool:
    path = _history_path()
    if path is None:
        return False
    reason = _normalize_reason(audit)
    if reason is None:
        return False
    provider_key = str(provider or "").strip().lower()
    asset_key = str(asset_id).strip()
    context_key = str(context_hash or "").strip()
    if not provider_key or not asset_key:
        return False
    scope = "global" if reason in _GLOBAL_REASONS else "context"
    if scope == "context" and not context_key:
        return False

    now = _now()
    history = _read_history(path)
    entries = _pruned(history.get(HISTORY_KEY), now=now)
    identity = (provider_key, asset_key, "" if scope == "global" else context_key, reason)
    retained: list[dict[str, Any]] = []
    prior_count = 0
    for entry in entries:
        entry_identity = (
            str(entry.get("provider")),
            str(entry.get("asset_id")),
            "" if entry.get("scope") == "global" else str(entry.get("context_hash") or ""),
            str(entry.get("reason")),
        )
        if entry_identity == identity:
            prior_count = max(prior_count, int(entry.get("failure_count") or 1))
            continue
        retained.append(entry)
    retained.insert(0, {
        "schema_version": SCHEMA_VERSION,
        "provider": provider_key,
        "asset_id": asset_key,
        "context_hash": None if scope == "global" else context_key,
        "scope": scope,
        "reason": reason,
        "recorded_at": now.isoformat(),
        "expires_at": (now + timedelta(days=max(1, int(ttl_days)))).isoformat(),
        "failure_count": prior_count + 1,
    })
    history[HISTORY_KEY] = retained[:MAX_ENTRIES]
    _write_history(path, history)
    return True


def install_into_engine() -> None:
    """Patch only VisualCandidateCache persistence semantics, not selection policy."""
    from isco_video_agent import visual_selection as selection

    cls = selection.VisualCandidateCache
    if getattr(cls, "_isco_persistent_failure_memory_v1", False):
        return
    original_get = cls.get
    original_set = cls.set

    def get(self, provider: str, asset_id: object, ctx_hash: str):
        cached = original_get(self, provider, asset_id, ctx_hash)
        if cached is not None:
            return cached
        learned = should_skip(provider, asset_id, ctx_hash)
        if learned is None:
            return None
        # Return a deterministic local block. This consumes no provider/Vision call.
        return {
            "status": "block",
            "reason": f"persistent_visual_failure_memory:{learned['reason']}",
            "relevance": 0.0,
            "visual_quality": 0.0,
            "persistent_failure_memory": True,
            "failure_scope": learned.get("scope"),
        }

    def set(self, provider: str, asset_id: object, ctx_hash: str, result: dict) -> None:
        original_set(self, provider, asset_id, ctx_hash, result)
        try:
            record_failure(provider, asset_id, ctx_hash, dict(result))
        except Exception:
            # Learning is an optimization; never mask the authoritative visual verdict.
            pass

    cls.get = get
    cls.set = set
    cls._isco_persistent_failure_memory_v1 = True
