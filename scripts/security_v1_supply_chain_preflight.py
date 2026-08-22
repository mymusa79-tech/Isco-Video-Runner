from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from isco_video_agent.supply_chain_integrity import (
    verify_critical_lock_completeness,
    verify_package_artifact,
    verify_piper_voice_files,
)


def verify_critical_artifacts(lock_path: Path) -> dict[str, str]:
    """Download, but do not install, each critical pinned wheel and verify its exact hash."""
    locked = verify_critical_lock_completeness(lock_path)
    verified: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="isco-security-wheels-") as tmp:
        wheel_dir = Path(tmp)
        for package, artifact in locked.items():
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "download",
                    "--disable-pip-version-check",
                    "--no-deps",
                    "--only-binary=:all:",
                    "--dest",
                    str(wheel_dir),
                    f"{package}=={artifact.version}",
                ],
                check=True,
            )
            expected = wheel_dir / artifact.filename
            verified[package] = verify_package_artifact(lock_path, package, expected)
    return verified


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    lock = sub.add_parser("lock")
    lock.add_argument("--lock", required=True, type=Path)

    voice = sub.add_parser("voice")
    voice.add_argument("--voice-dir", required=True, type=Path)
    voice.add_argument("--manifest", required=True, type=Path)

    args = parser.parse_args()
    if args.command == "lock":
        verified = verify_critical_artifacts(args.lock)
        print("Security V1 critical package artifacts verified:", ",".join(sorted(verified)))
        return

    verified = verify_piper_voice_files(args.voice_dir, args.manifest)
    print("Security V1 Piper voice files verified:", ",".join(sorted(verified)))


if __name__ == "__main__":
    main()
