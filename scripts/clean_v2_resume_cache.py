from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_relative(raw: object) -> Path:
    relative = Path(str(raw))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise RuntimeError("unsafe_clean_v2_resume_artifact_path")
    return relative


def _read_checkpoint(root: Path) -> dict[str, Any] | None:
    path = root / "resume-checkpoint.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        return None
    try:
        for raw, expected in artifacts.items():
            relative = _safe_relative(raw)
            artifact = root / relative
            if (
                artifact.is_symlink()
                or not artifact.is_file()
                or artifact.stat().st_size <= 0
                or _sha256_file(artifact) != str(expected)
            ):
                return None
    except (OSError, RuntimeError):
        return None
    return value


def prepare_resume_cache(output: Path, target: Path) -> bool:
    """Atomically persist only artifacts authenticated by resume-checkpoint.json."""
    output = Path(output)
    target = Path(target)
    checkpoint = _read_checkpoint(output)
    if checkpoint is None:
        return _read_checkpoint(target) is not None

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent)
    )
    try:
        shutil.copy2(
            output / "resume-checkpoint.json",
            temporary / "resume-checkpoint.json",
        )
        for raw in checkpoint["artifacts"]:
            relative = _safe_relative(raw)
            destination = temporary / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output / relative, destination)
        if _read_checkpoint(temporary) is None:
            raise RuntimeError("prepared_clean_v2_resume_cache_failed_validation")
        previous = target.with_name(f".{target.name}.previous")
        if previous.exists():
            shutil.rmtree(previous, ignore_errors=True)
        if target.exists():
            os.replace(target, previous)
        os.replace(temporary, target)
        shutil.rmtree(previous, ignore_errors=True)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        return _read_checkpoint(target) is not None
    return True


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare a verified Clean V2 resume cache"
    )
    parser.add_argument("command", choices=("prepare",))
    parser.add_argument("--output", required=True)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    allowed = prepare_resume_cache(Path(args.output), Path(args.target))
    print(f"save_allowed={'true' if allowed else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
