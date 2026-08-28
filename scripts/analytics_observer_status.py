from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


STATUS_FILENAME = "analytics-observer-status.json"


def _write_status(output_dir: Path, payload: dict) -> Path:
    path = Path(output_dir) / STATUS_FILENAME
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def observe_post_acceptance_analytics(
    output_dir: Path,
    *,
    collector: Callable[..., object],
    format_hint: str,
    expected_video_id: str | None,
    production_id: str | None,
    binding_source: str | None,
) -> dict:
    """Run YouTube analytics as observable, strictly non-authoritative post-Gold work.

    The observer must never change release authority. A collector error is converted
    into durable status evidence rather than being silently swallowed or promoted into
    a production failure. Error text is intentionally not persisted because provider
    exceptions can contain request/credential context; the exception class is enough
    to distinguish an observer failure from a skipped/unbound observation safely.
    """
    base = {
        "schema_version": 1,
        "mode": "observe_only",
        "production_authority": "none",
        "stage": "post_gold_post_manifest",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "collector": "youtube_analytics.collect_latest_video_metrics_from_env",
        "format_hint": str(format_hint or ""),
        "expected_video_id": str(expected_video_id or "") or None,
        "production_id": str(production_id or "") or None,
        "binding_source": str(binding_source or "") or None,
    }
    try:
        collector(
            format_hint=format_hint,
            expected_video_id=expected_video_id,
            production_id=production_id,
            binding_source=binding_source,
        )
    except Exception as exc:
        payload = {
            **base,
            "status": "error",
            "error_type": type(exc).__name__,
            "release_blocked": False,
        }
        _write_status(output_dir, payload)
        print(f"YouTube analytics observer error recorded: {type(exc).__name__}")
        return payload

    payload = {
        **base,
        "status": "success",
        "error_type": None,
        "release_blocked": False,
    }
    _write_status(output_dir, payload)
    print("YouTube analytics observer status: success")
    return payload
