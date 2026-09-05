from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.telegram_control_active_ui import _mark_request_used
from scripts.telegram_production_queue import QUEUE_KEY, release_tag_for, validate_ready_request


def reconcile_completed_history(state: dict[str, Any]) -> dict[str, int]:
    """Backfill Used history from authoritative completed Telegram dispatch receipts.

    Only exact completed queue records are eligible. Each record must still bind to
    the immutable approved request hash and deterministic release tag. The operation
    is idempotent because _mark_request_used upserts by request_id.

    ``production_queue`` is a lazily-created durable ledger within state schema v1,
    so a valid fresh or pre-ledger state may legitimately omit it until the first
    dispatch. Canonicalize only absence to an empty ledger; an explicitly malformed
    value remains fail-closed.
    """
    queue = state.setdefault(QUEUE_KEY, [])
    requests = state.get("requests")
    if not isinstance(queue, list):
        raise RuntimeError("Telegram production queue is malformed")
    if not isinstance(requests, dict):
        raise RuntimeError("Telegram request registry is malformed")

    existing_ids = {
        str(item.get("request_id") or "")
        for item in (state.get("used_topics") if isinstance(state.get("used_topics"), list) else [])
        if isinstance(item, dict)
    }
    processed = 0
    added = 0
    for entry in queue:
        if not isinstance(entry, dict) or str(entry.get("status") or "") != "completed":
            continue
        request_id = str(entry.get("request_id") or "").strip()
        request_sha256 = str(entry.get("request_sha256") or "").strip()
        release_tag = str(entry.get("completed_release_tag") or "").strip()
        completed_at = str(entry.get("completed_at") or "").strip()
        if not request_id or not request_sha256 or not release_tag or not completed_at:
            raise RuntimeError("Completed Telegram dispatch receipt is incomplete")
        request = requests.get(request_id)
        if not isinstance(request, dict):
            raise RuntimeError("Completed Telegram dispatch has no approved request")
        validate_ready_request(request)
        if str(request.get("request_sha256") or "") != request_sha256:
            raise RuntimeError("Completed Telegram dispatch hash does not match approved request")
        if release_tag_for(request) != release_tag:
            raise RuntimeError("Completed Telegram dispatch release tag is not deterministic for request")
        _mark_request_used(
            state,
            request,
            release_tag=release_tag,
            used_at=completed_at,
        )
        processed += 1
        if request_id not in existing_ids:
            existing_ids.add(request_id)
            added += 1

    return {"processed": processed, "added": added}


def reconcile_file(path: Path) -> dict[str, int]:
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise RuntimeError("Telegram control state must be an object")
    result = reconcile_completed_history(state)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True, type=Path)
    args = parser.parse_args()
    result = reconcile_file(args.state)
    print(
        "Telegram Used history reconciliation: "
        f"processed={result['processed']} added={result['added']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
