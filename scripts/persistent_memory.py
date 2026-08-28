from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from scripts import persistent_memory_core as _core
    from scripts.planning_checkpoint_state import bootstrap_runtime_restore, canonical_runtime_enabled
except ModuleNotFoundError:  # direct `python scripts/persistent_memory.py`
    import persistent_memory_core as _core
    from planning_checkpoint_state import bootstrap_runtime_restore, canonical_runtime_enabled


for _name in dir(_core):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_core, _name)


def main(argv: list[str] | None = None) -> int:
    args = _core.build_parser().parse_args(argv)
    result = int(args.func(args))
    if result == 0 and args.command == "restore" and canonical_runtime_enabled():
        key = (os.environ.get("STATE_ENCRYPTION_KEY") or "").strip()
        if not key:
            raise RuntimeError("STATE_ENCRYPTION_KEY is required for durable planning checkpoint restore")
        repo_root = Path(args.repo).resolve()
        bootstrap_runtime_restore(
            repo_root=repo_root,
            engine_root=repo_root / "engine",
            key=key,
        )
    return result


_core.main = main

if __name__ == "__main__":
    raise SystemExit(main())

# Imported callers receive the exact original implementation module with only main()
# wrapped, preserving private helpers/monkeypatch behavior used by existing regression tests.
sys.modules[__name__] = _core
