from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.telegram_production_queue import validate_dispatch_authorization


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def defer_reserved_dispatch(
    state: dict[str, Any],
    request_id: str,
    request_sha256: str,
    authorization_id: str,
    *,
    runner_sha: str,
) -> dict[str, Any]:
    item = validate_dispatch_authorization(
        state,
        request_id,
        request_sha256,
        authorization_id,
        runner_sha=runner_sha,
    )
    deferred_at = _now()
    previous_reserved_at = str(item.pop("reserved_at", "") or "")
    previous_runner_sha = str(item.pop("runner_sha", "") or "")

    item["status"] = "pending_dispatch"
    item["certification_deferred_at"] = deferred_at
    item["certification_defer_count"] = int(item.get("certification_defer_count", 0) or 0) + 1
    item["certification_defer_reason"] = "production_certification_pending"
    if previous_reserved_at:
        item["last_reserved_at"] = previous_reserved_at
    if previous_runner_sha:
        item["last_reserved_runner_sha"] = previous_runner_sha
    state["last_event_at"] = deferred_at
    return item


def main() -> int:
    parser = argparse.ArgumentParser(description="Return an exact reserved Telegram production to the durable pending queue")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--runner-sha", required=True)
    args = parser.parse_args()

    state = json.loads(args.state.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise RuntimeError("Telegram control state must be an object")
    item = defer_reserved_dispatch(
        state,
        args.request_id,
        args.sha256,
        args.authorization_id,
        runner_sha=args.runner_sha,
    )
    args.state.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
