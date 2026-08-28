from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


STATUS_FILENAME = "analytics-observer-status.json"


def _persist_status_best_effort(output_dir: Path, payload: dict) -> dict:
    path = Path(output_dir) / STATUS_FILENAME
    tmp = path.with_suffix(path.suffix + ".tmp")
    enriched = dict(payload)
    try:
        tmp.write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        enriched["status_persisted"] = False
        enriched["status_persist_error_type"] = type(exc).__name__
        print(f"YouTube analytics observer sidecar write failed: {type(exc).__name__}")
        return enriched
    enriched["status_persisted"] = True
    enriched["status_persist_error_type"] = None
    # Rewrite once with its own persistence state included. Failure here is still
    # non-authoritative; telemetry receives the returned in-memory evidence below.
    try:
        tmp.write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        enriched["status_persisted"] = False
        enriched["status_persist_error_type"] = type(exc).__name__
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
    return enriched


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
        payload = _persist_status_best_effort(
            output_dir,
            {
                **base,
                "status": "error",
                "error_type": type(exc).__name__,
                "release_blocked": False,
            },
        )
        print(f"YouTube analytics observer error recorded: {type(exc).__name__}")
        return payload

    payload = _persist_status_best_effort(
        output_dir,
        {
            **base,
            "status": "success",
            "error_type": None,
            "release_blocked": False,
        },
    )
    print("YouTube analytics observer status: success")
    return payload
