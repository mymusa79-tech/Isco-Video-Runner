from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from scripts import persistent_memory_core as _core
    from scripts.immutable_planning_snapshot import bootstrap_immutable_planning_checkpoint
    from scripts.runtime_phase import activate_canonical_runtime, canonical_workflow_identity
except ModuleNotFoundError:  # direct `python scripts/persistent_memory.py`
    import persistent_memory_core as _core
    from immutable_planning_snapshot import bootstrap_immutable_planning_checkpoint
    from runtime_phase import activate_canonical_runtime, canonical_workflow_identity


for _name in dir(_core):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_core, _name)


def main(argv: list[str] | None = None) -> int:
    args = _core.build_parser().parse_args(argv)
    result = int(args.func(args))
    if result == 0 and args.command == "restore" and canonical_workflow_identity():
        key = (os.environ.get("STATE_ENCRYPTION_KEY") or "").strip()
        if not key:
            raise RuntimeError("STATE_ENCRYPTION_KEY is required for durable planning checkpoint restore")
        repo_root = Path(args.repo).resolve()
        # Run130 closure: workflow identity is only context. The application owns the
        # exact transition from certified pre-production into live runtime. Activate
        # here, after the pre-production suites have completed and immediately before
        # the immutable production snapshot/durable checkpoint bootstrap.
        activate_canonical_runtime()
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
