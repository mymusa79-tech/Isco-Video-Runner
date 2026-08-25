from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

try:
    from scripts.persistent_memory import persist_encrypted_state
except ModuleNotFoundError:  # direct `python scripts/state_persistence_strict.py`
    from persistent_memory import persist_encrypted_state


def persist_strict(*, repo: Path, encrypted: Path, branch: str, run_number: str, report: Path) -> None:
    status = persist_encrypted_state(
        repo,
        encrypted,
        branch=branch,
        run_number=run_number,
        key=os.environ.get("STATE_ENCRYPTION_KEY", ""),
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    tmp = report.with_name(report.name + ".tmp")
    tmp.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "authenticated_envelope_required": True,
                "pushed": status.pushed,
                "changed": status.changed,
                "reason": status.reason,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    tmp.replace(report)
    if not status.pushed:
        raise RuntimeError("accepted production state was not durably persisted")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--encrypted", type=Path, required=True)
    parser.add_argument("--branch", default="agent-state")
    parser.add_argument("--run-number", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    persist_strict(
        repo=args.repo,
        encrypted=args.encrypted,
        branch=args.branch,
        run_number=args.run_number,
        report=args.report,
    )
    print("Persistent authenticated state closure PASS")


if __name__ == "__main__":
    main()
