from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
_BOOTSTRAP_TIME = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _hash_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _available_saved_count(state: dict[str, Any]) -> int:
    count = 0
    for item in _list(state.get("saved_suggestions")):
        if isinstance(item, dict) and str(item.get("status") or "") == "available":
            count += 1
    return count


def _event_time(value: Any) -> datetime | None:
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


def build_projection(state: dict[str, Any], *, generated_at: datetime | None = None) -> dict[str, Any]:
    # Use the control state's last meaningful event time by default. Empty scheduled
    # health runs therefore do not create a public Git commit every five minutes.
    now = generated_at or _event_time(state.get("last_event_at")) or _BOOTSTRAP_TIME
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    active_session = str(state.get("active_research_session_id") or "").strip()
    target = _dict(state.get("production_target"))
    requests = _dict(state.get("requests"))
    queue = _list(state.get("production_queue"))
    pending_actions = _list(state.get("pending_actions"))

    projection = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "source_last_event_at": str(state.get("last_event_at") or ""),
        "editorial": {
            "research_active": bool(active_session),
            "research_session_hash": _hash_id(active_session),
            "saved_count": _available_saved_count(state),
            "used_count": len([item for item in _list(state.get("used_topics")) if isinstance(item, dict)]),
            "request_count": len(requests),
            "pending_actions_count": len(pending_actions),
            "production_queue_count": len(queue),
            "approved_target": bool(target),
            "approved_request_hash": _hash_id(target.get("request_id")),
        },
    }
    return projection


def write_projection(state_path: Path, output_path: Path) -> None:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise RuntimeError("Telegram control state must be an object")
    projection = build_projection(state)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(projection, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    write_projection(Path(args.state), Path(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
