from __future__ import annotations

import os
import sys

try:
    from scripts import environment_preflight_core as _core
    from scripts.planning_checkpoint_state import materialize_runtime_github_token
    from scripts.runtime_phase import canonical_workflow_identity
    from scripts.stage_ladder_gate import require_exact_sha_stage_ladder
except ModuleNotFoundError:  # direct `python scripts/environment_preflight.py`
    import environment_preflight_core as _core
    from planning_checkpoint_state import materialize_runtime_github_token
    from runtime_phase import canonical_workflow_identity
    from stage_ladder_gate import require_exact_sha_stage_ladder


for _name in dir(_core):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_core, _name)


_core_original_main = _core.main


def main() -> None:
    _core_original_main()
    # P0 Runtime Master V2: exact-SHA certification and credential staging belong to
    # canonical pre-production preparation, not to the live runtime phase. This step
    # already owns GITHUB_TOKEN and runs before provider work. Keeping that preparation
    # here lets the real runtime transition remain false until all readiness gates have
    # passed, while durable planning still receives a file-backed credential later.
    if canonical_workflow_identity():
        token = (os.environ.get("GITHUB_TOKEN") or "").strip()
        if not token:
            raise RuntimeError("GITHUB_TOKEN is required for canonical production preflight")
        tag = require_exact_sha_stage_ladder(
            repository=os.environ.get("GITHUB_REPOSITORY") or "",
            sha=os.environ.get("GITHUB_SHA") or "",
            token=token,
        )
        print(f"Exact-SHA Production Stage Ladder certification PASS: {tag}")
        materialize_runtime_github_token(token)
        print("Durable planning checkpoint GitHub credential materialized for this job")


_core.main = main

if __name__ == "__main__":
    main()

sys.modules[__name__] = _core