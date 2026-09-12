from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

try:
    from scripts.gold_resume_workflow_identity import gold_resume_workflow_identity
    from scripts.persistent_memory import persist_encrypted_state
    from scripts.persistent_memory_crypto import open_envelope
except ModuleNotFoundError:  # direct `python scripts/state_persistence_strict.py`
    from gold_resume_workflow_identity import gold_resume_workflow_identity
    from persistent_memory import persist_encrypted_state
    from persistent_memory_crypto import open_envelope


def _effective_run_number(encrypted: Path, requested_run_number: str) -> str:
    """Use authenticated state sequence for the cross-workflow Gold resume only.

    GITHUB_RUN_NUMBER is workflow-local, so Resume Gold starts again at 1 and cannot
    safely identify the shared agent-state revision. The encrypted envelope created by
    persistent_memory.py already advances from the authenticated prior state by one;
    consume that authenticated sequence here instead of regressing to the resume job's
    local counter. All canonical production workflows retain the existing exact-run
    equality contract.
    """
    if not gold_resume_workflow_identity():
        return str(requested_run_number)
    key = os.environ.get("STATE_ENCRYPTION_KEY", "")
    _, metadata = open_envelope(Path(encrypted).read_bytes(), key)
    return str(metadata.sequence)


def persist_strict(*, repo: Path, encrypted: Path, branch: str, run_number: str, report: Path) -> None:
    effective_run_number = _effective_run_number(encrypted, run_number)
    status = persist_encrypted_state(
        repo,
        encrypted,
        branch=branch,
        run_number=effective_run_number,
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
                "requested_run_number": str(run_number),
                "effective_state_sequence": effective_run_number,
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
