from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from scripts.telegram_production_queue import dispatch_entry_is_live


def reserved_dispatch_for_runner(state: dict[str, Any], runner_sha: str) -> dict[str, Any] | None:
    runner_sha = str(runner_sha or "").strip().lower()
    if len(runner_sha) != 40 or any(ch not in "0123456789abcdef" for ch in runner_sha):
        raise RuntimeError("Certification resume requires an exact 40-hex Runner SHA")
    queue = state.get("production_queue")
    if not isinstance(queue, list):
        raise RuntimeError("Telegram production queue is malformed")
    matches = [
        item
        for item in queue
        if isinstance(item, dict)
        and item.get("status") == "dispatch_reserved"
        and str(item.get("runner_sha") or "").strip().lower() == runner_sha
        and dispatch_entry_is_live(item)
    ]
    if len(matches) > 1:
        raise RuntimeError("Multiple live Telegram reservations are bound to the same Runner SHA")
    if not matches:
        return None
    item = matches[0]
    for field in ("request_id", "request_sha256", "authorization_id"):
        if not str(item.get(field) or "").strip():
            raise RuntimeError(f"Certification resume reservation lacks {field}")
    return item


def _write_outputs(path: Path, item: dict[str, Any] | None) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"found={'true' if item else 'false'}\n")
        if item:
            handle.write(f"request_id={item['request_id']}\n")
            handle.write(f"request_sha256={item['request_sha256']}\n")
            handle.write(f"authorization_id={item['authorization_id']}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Select one exact reserved Telegram production for certification resume")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--runner-sha", required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    state = json.loads(args.state.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise RuntimeError("Telegram control state must be an object")
    item = reserved_dispatch_for_runner(state, args.runner_sha)
    if args.github_output is not None:
        _write_outputs(args.github_output, item)
    print(json.dumps(item or {"found": False}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
