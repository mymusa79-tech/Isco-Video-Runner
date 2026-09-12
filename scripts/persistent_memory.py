from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from scripts import persistent_memory_core as _core
    from scripts.gold_resume_workflow_identity import gold_resume_workflow_identity
    from scripts.immutable_planning_snapshot import bootstrap_immutable_planning_checkpoint
    from scripts.runtime_phase import activate_canonical_runtime, canonical_workflow_identity
except ModuleNotFoundError:  # direct `python scripts/persistent_memory.py`
    import persistent_memory_core as _core
    from gold_resume_workflow_identity import gold_resume_workflow_identity
    from immutable_planning_snapshot import bootstrap_immutable_planning_checkpoint
    from runtime_phase import activate_canonical_runtime, canonical_workflow_identity


for _name in dir(_core):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_core, _name)


def _positive_int(value: object) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _gold_resume_next_state_sequence(plain_path: Path) -> str:
    """Return a monotonic agent-state sequence for the cross-workflow Gold resume.

    GitHub's GITHUB_RUN_NUMBER is scoped per workflow, so it is not part of the shared
    agent-state ordering relation. The Gold-resume workflow therefore advances exactly
    one revision from the authenticated restore identity. If no authenticated sequence
    exists yet, the first shared state revision is 1. Canonical production keeps its
    existing run-number semantics unchanged.
    """
    identity = _core.read_restore_identity(Path(plain_path))
    previous = _positive_int(identity.get("state_sequence"))
    return str((previous + 1) if previous is not None else 1)


def main(argv: list[str] | None = None) -> int:
    args = _core.build_parser().parse_args(argv)

    # Run #246 closure: authenticated memory ordering cannot compare run numbers from
    # different GitHub workflows. For Gold-only resume, restore the latest authenticated
    # state by commit ancestry/authentication without applying the canonical-production
    # run-number freshness check. Any later write receives a new monotonic sequence below.
    removed_run_number: str | None = None
    removed = False
    if args.command == "restore" and gold_resume_workflow_identity() and "GITHUB_RUN_NUMBER" in os.environ:
        removed_run_number = os.environ.pop("GITHUB_RUN_NUMBER")
        removed = True

    if args.command == "encrypt" and gold_resume_workflow_identity() and args.run_number is None:
        args.run_number = _gold_resume_next_state_sequence(Path(args.plain))

    try:
        result = int(args.func(args))
    finally:
        if removed:
            os.environ["GITHUB_RUN_NUMBER"] = str(removed_run_number or "")

    if result == 0 and args.command == "restore" and canonical_workflow_identity():
        key = (os.environ.get("STATE_ENCRYPTION_KEY") or "").strip()
        if not key:
            raise RuntimeError("STATE_ENCRYPTION_KEY is required for durable planning checkpoint restore")
        repo_root = Path(args.repo).resolve()
        # P0 Runtime Master V2: this process is still pre-production. We need the
        # canonical-runtime helper semantics only long enough to freeze the approved
        # brief, restore the Telegram-mutated Engine fixture to its pinned bytes, and
        # authenticate the durable planning checkpoint. Do NOT export live-runtime
        # authority into later workflow steps: environment/provider/planning preflights
        # must complete before the production entry process performs the real phase
        # transition.
        activate_canonical_runtime(persist_workflow_env=False)
        bootstrap_immutable_planning_checkpoint(
            repo_root=repo_root,
            engine_root=repo_root / "engine",
            encryption_key=key,
        )
    return result


_core.main = main

if __name__ == "__main__":
    raise SystemExit(main())

# Imported callers receive the exact original implementation module with only main()
# wrapped, preserving private helpers/monkeypatch behavior used by existing regression tests.
sys.modules[__name__] = _core
