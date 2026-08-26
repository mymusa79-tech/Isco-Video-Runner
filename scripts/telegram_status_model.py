from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_CONTRACT_PATH = Path(__file__).with_name("telegram_status_contract.json")


@lru_cache(maxsize=1)
def contract() -> dict[str, Any]:
    value = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or int(value.get("schema_version") or 0) != 1:
        raise RuntimeError("Unsupported Telegram status contract")
    return value


def status_label(value: Any, default: str = "غير محدد") -> str:
    raw = str(value or "").strip()
    if not raw:
        return default
    labels = contract().get("status_labels") or {}
    return str(labels.get(raw.casefold()) or raw)


def terminal_state(conclusion: Any) -> dict[str, str] | None:
    value = str(conclusion or "").strip().casefold()
    item = (contract().get("run_terminal") or {}).get(value)
    if not isinstance(item, dict):
        return None
    return {"label": str(item.get("label") or value), "icon": str(item.get("icon") or "")}


def stage_for_step(step_name: Any) -> dict[str, str]:
    raw = str(step_name or "").strip()
    folded = raw.casefold()
    for rule in contract().get("stage_rules") or []:
        if not isinstance(rule, dict):
            continue
        needles = [str(item).casefold() for item in rule.get("contains") or [] if str(item).strip()]
        if folded and any(needle in folded for needle in needles):
            return {"key": str(rule.get("key") or "unknown"), "label": str(rule.get("label") or raw or "الإنتاج الجاري")}
    return {"key": "unknown", "label": raw or "الإنتاج الجاري"}


def lifecycle_labels() -> dict[str, str]:
    labels: dict[str, str] = {}
    for key in contract().get("lifecycle_order") or []:
        labels[str(key)] = status_label(key, str(key))
    return labels


def projection_freshness(age_seconds: float | int | None) -> str:
    if age_seconds is None:
        return "unavailable"
    age = max(0.0, float(age_seconds))
    freshness = contract().get("freshness") or {}
    fresh_limit = max(1.0, float(freshness.get("projection_fresh_seconds") or 600))
    stale_limit = max(fresh_limit, float(freshness.get("projection_stale_seconds") or 3600))
    if age <= fresh_limit:
        return "fresh"
    if age <= stale_limit:
        return "stale"
    return "expired"
