from __future__ import annotations

"""Prepare a failure-memory-only durable state checkpoint.

The Engine owns the meaning and lifecycle of ``visual_failure_memory``. The Runner owns
cross-run state transport. When a production cannot persist its full accepted history,
this module copies only the Engine-owned failure-memory envelope onto the immutable
restored history baseline. Runtime ``videos`` or any other unaccepted state changes are
never carried into the checkpoint.
"""

import argparse
import json
from pathlib import Path
from typing import Any

MEMORY_KEY = "visual_failure_memory"
MEMORY_SCHEMA = "isco.visual-failure-memory.v1"
MAX_ENTRIES = 512
MAX_MEMORY_BYTES = 2 * 1024 * 1024


def _read_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("not a regular file")
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"visual failure checkpoint requires valid {label}: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"visual failure checkpoint requires object {label}")
    videos = value.get("videos", [])
    if not isinstance(videos, list):
        raise RuntimeError(f"visual failure checkpoint requires {label}.videos array")
    return value


def _validated_memory(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RuntimeError("visual failure checkpoint memory must be an object")
    if value.get("schema") != MEMORY_SCHEMA:
        raise RuntimeError("visual failure checkpoint memory schema mismatch")
    entries = value.get("entries")
    if not isinstance(entries, list):
        raise RuntimeError("visual failure checkpoint memory entries must be an array")
    if len(entries) > MAX_ENTRIES:
        raise RuntimeError("visual failure checkpoint memory exceeds Engine entry cap")
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RuntimeError(f"visual failure checkpoint entry {index} is not an object")
        provider = str(entry.get("provider") or "").strip().lower()
        asset_id = str(entry.get("asset_id") or "").strip()
        scope = str(entry.get("scope") or "").strip().lower()
        reason_code = str(entry.get("reason_code") or "").strip()
        expires_at = str(entry.get("expires_at") or "").strip()
        if not provider or not asset_id or scope not in {"contextual", "asset_global"}:
            raise RuntimeError(f"visual failure checkpoint entry {index} identity is invalid")
        if not reason_code or not expires_at:
            raise RuntimeError(f"visual failure checkpoint entry {index} lacks reason/expiry")
        if scope == "contextual" and not str(entry.get("context_hash") or "").strip():
            raise RuntimeError(f"visual failure checkpoint contextual entry {index} lacks context hash")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_MEMORY_BYTES:
        raise RuntimeError("visual failure checkpoint memory exceeds byte cap")
    return dict(value)


def prepare_failure_memory_checkpoint(
    *,
    baseline_path: Path,
    runtime_path: Path,
    output_path: Path,
) -> bool:
    """Write baseline + runtime failure memory only. Return whether memory changed."""
    baseline = _read_object(Path(baseline_path), label="restored baseline")
    runtime = _read_object(Path(runtime_path), label="runtime history")
    baseline_memory = _validated_memory(baseline.get(MEMORY_KEY))
    runtime_memory = _validated_memory(runtime.get(MEMORY_KEY))

    if runtime_memory is None or runtime_memory == baseline_memory:
        try:
            Path(output_path).unlink()
        except FileNotFoundError:
            pass
        return False

    checkpoint = dict(baseline)
    checkpoint[MEMORY_KEY] = runtime_memory

    # Defense in depth: no runtime root field other than visual_failure_memory may enter
    # the failure-only checkpoint. In particular, a video appended before a later gate
    # failure must never become accepted history through this path.
    for key, value in checkpoint.items():
        if key == MEMORY_KEY:
            continue
        if baseline.get(key) != value:
            raise RuntimeError(f"visual failure checkpoint changed forbidden baseline field: {key}")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + ".tmp")
    temp.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(output)
    return True


def _append_output(path: str | None, *, changed: bool, output_path: Path) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(f"changed={'true' if changed else 'false'}\n")
        handle.write(f"checkpoint_path={output_path if changed else ''}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare a failure-memory-only durable state checkpoint")
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output", default=None)
    args = parser.parse_args(argv)
    changed = prepare_failure_memory_checkpoint(
        baseline_path=args.baseline,
        runtime_path=args.runtime,
        output_path=args.output,
    )
    _append_output(args.github_output, changed=changed, output_path=args.output)
    print(f"Visual failure memory checkpoint: changed={'true' if changed else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
