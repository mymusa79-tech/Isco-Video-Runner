#!/usr/bin/env python3
"""Rebuild a verified voice-bank resume cache from a finished run's artifact.

For runs whose post-render layers rewrote cta-plan.json / rights-manifest.json
(so the original checkpoint no longer validated and no cache was saved). Only
byte-identical artifacts are accepted; the pristine cta-plan.json comes from
owner-pack-staging and must match the hash recorded in the original checkpoint.
No provider call, no regeneration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DROPPED = ("rights-manifest.json",)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rescue(source: Path, target: Path) -> Path:
    from clean_v2 import pipeline
    from scripts.clean_v2_resume_cache import prepare_resume_cache

    source, target = Path(source), Path(target)
    original = json.loads((source / "resume-checkpoint.json").read_text(encoding="utf-8"))
    identity = original["identity"]
    artifacts = original["artifacts"]
    staging = source / "owner-pack-staging"
    work = target.parent / f".{target.name}-rescue"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    for raw, expected in artifacts.items():
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(f"unsafe artifact path: {raw}")
        if raw in DROPPED or raw.startswith("visuals/"):
            continue
        candidate = source / relative
        if not candidate.is_file() or _sha(candidate) != expected:
            alternate = staging / relative
            if alternate.is_file() and _sha(alternate) == expected:
                candidate = alternate
            else:
                raise RuntimeError(f"artifact cannot be proven identical: {raw}")
        destination = work / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate, destination)
    pipeline._write_resume_checkpoint(
        work,
        completed_stage="voice",
        approved_brief_sha256=identity["approved_brief_sha256"],
        engine_sha=identity["engine_sha"],
        runner_sha=identity.get("runner_sha"),
        max_visuals=int(identity["max_visuals"]),
        voice_provider=original["voice_provider"],
        voice_fallback_used=original["voice_fallback_used"],
    )
    if not prepare_resume_cache(work, target):
        raise RuntimeError("rescued checkpoint failed validation")
    shutil.rmtree(work, ignore_errors=True)
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="run dir holding resume-checkpoint.json")
    parser.add_argument("--target", required=True, help="CLEAN_V2_RESUME/<format>")
    args = parser.parse_args()
    print(f"rescued={rescue(Path(args.source), Path(args.target))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
