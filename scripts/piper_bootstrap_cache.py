from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from isco_video_agent.supply_chain_integrity import (
    SupplyChainIntegrityError,
    load_piper_voice_manifest,
    verify_piper_voice_files,
)


_WHEEL_NAME_RE = re.compile(r"^piper_tts-1\.4\.2-[A-Za-z0-9_.+-]+\.whl$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PiperBootstrapCacheError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise PiperBootstrapCacheError(f"cache entry is not a regular file: {path.name}")


def validate_wheel_directory(directory: str | Path, expected_sha256: str) -> dict[str, str]:
    root = Path(directory)
    expected_sha256 = str(expected_sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(expected_sha256):
        raise PiperBootstrapCacheError("invalid expected Piper wheel sha256")
    if root.is_symlink() or not root.is_dir():
        raise PiperBootstrapCacheError("Piper wheel cache directory missing or unsafe")

    entries = list(root.iterdir())
    if len(entries) != 1:
        raise PiperBootstrapCacheError(
            f"Piper wheel cache must contain exactly one entry, found {len(entries)}"
        )
    wheel = entries[0]
    _require_regular_file(wheel)
    if not _WHEEL_NAME_RE.fullmatch(wheel.name):
        raise PiperBootstrapCacheError(f"unexpected Piper wheel filename: {wheel.name}")
    actual = _sha256(wheel)
    if actual != expected_sha256:
        raise PiperBootstrapCacheError(
            f"Piper wheel sha256 mismatch: expected={expected_sha256} actual={actual}"
        )
    return {"wheel": str(wheel), "sha256": actual}


def validate_voice_directory(
    directory: str | Path,
    manifest_path: str | Path,
) -> dict[str, str]:
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise PiperBootstrapCacheError("Piper voice cache directory missing or unsafe")

    manifest = load_piper_voice_manifest(manifest_path)
    expected_names = {entry.filename for entry in manifest.files}
    entries = list(root.iterdir())
    actual_names = {entry.name for entry in entries}
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        raise PiperBootstrapCacheError(
            f"Piper voice cache shape mismatch: missing={missing} extra={extra}"
        )
    for entry in entries:
        _require_regular_file(entry)

    verified = verify_piper_voice_files(root, manifest_path)
    return {name: digest for name, digest in sorted(verified.items())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate untrusted Piper bootstrap cache")
    sub = parser.add_subparsers(dest="command", required=True)

    wheel = sub.add_parser("wheel")
    wheel.add_argument("--directory", required=True, type=Path)
    wheel.add_argument("--sha256", required=True)

    voice = sub.add_parser("voice")
    voice.add_argument("--directory", required=True, type=Path)
    voice.add_argument("--manifest", required=True, type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "wheel":
            result = validate_wheel_directory(args.directory, args.sha256)
        else:
            result = validate_voice_directory(args.directory, args.manifest)
    except (PiperBootstrapCacheError, SupplyChainIntegrityError, OSError, UnicodeError) as exc:
        print(f"Piper bootstrap cache INVALID: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({"status": "green", "verified": result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
