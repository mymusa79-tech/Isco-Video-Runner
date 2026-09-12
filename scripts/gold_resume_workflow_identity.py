from __future__ import annotations

import os


_GOLD_RESUME_WORKFLOW_MARKER = "/.github/workflows/resume-gold-qc-pending.yml@"


def gold_resume_workflow_identity() -> bool:
    """Authenticate the exact GitHub workflow allowed to use cross-workflow state sequencing.

    The Gold-resume path intentionally relaxes only the meaningless comparison against
    its own workflow-local GITHUB_RUN_NUMBER. Bind that exception to the immutable
    workflow file identity rather than the human-readable workflow name, which is not a
    security boundary and can be duplicated by another workflow.
    """
    return (
        str(os.environ.get("GITHUB_ACTIONS") or "").strip().lower() == "true"
        and str(os.environ.get("GITHUB_EVENT_NAME") or "").strip() == "workflow_dispatch"
        and _GOLD_RESUME_WORKFLOW_MARKER in str(os.environ.get("GITHUB_WORKFLOW_REF") or "")
    )
