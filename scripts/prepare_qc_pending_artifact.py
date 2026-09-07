from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from scripts.qc_pending_resume_bundle_v1 import build_resume_bundle


def _github_output(path: Path | None, **values: object) -> None:
    if path is None:
        return
    with Path(path).open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def prepare(output_parent: Path, destination: Path, *, github_output: Path | None = None) -> dict[str, Any] | None:
    roots = [path for path in Path(output_parent).glob("*") if path.is_dir() and (path / "qc-pending.json").is_file()]
    if not roots:
        _github_output(github_output, ready="false", artifact_name="", bundle_path="", checkpoint_path="")
        print("QC_PENDING resume artifact: no exact checkpoint found")
        return None
    if len(roots) != 1:
        raise RuntimeError(f"QC_PENDING resume artifact requires exactly one checkpointed output, found {len(roots)}")

    root = roots[0].resolve()
    manifest = build_resume_bundle(root, Path(destination))
    source = manifest.get("source") or {}
    run_id = str(source.get("run_id") or "").strip()
    run_attempt = str(source.get("run_attempt") or "").strip()
    artifact_name = f"isco-qc-pending-{run_id}-{run_attempt}"
    _github_output(
        github_output,
        ready="true",
        artifact_name=artifact_name,
        bundle_path=str(Path(destination).resolve()),
        checkpoint_path=str((root / "qc-pending.json").resolve()),
        output_dir=str(root),
        source_run_id=run_id,
        source_run_attempt=run_attempt,
        runner_sha=str(source.get("runner_sha") or ""),
        engine_sha=str(source.get("engine_sha") or ""),
        final_sha256=str(manifest.get("final_sha256") or ""),
    )
    print(json.dumps({"artifact_name": artifact_name, "manifest": manifest}, ensure_ascii=False, sort_keys=True))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-parent", type=Path, default=Path("engine/output"))
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    prepare(args.output_parent, args.destination, github_output=args.github_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
