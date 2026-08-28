from __future__ import annotations

import os
from pathlib import Path


_CANONICAL_WORKFLOW_MARKER = "/.github/workflows/produce-resilient-v4.yml@"
_RUNTIME_ENV = "ISCO_CANONICAL_RUNTIME"
_TRUE_VALUES = {"1", "true", "yes", "on"}


def canonical_workflow_identity() -> bool:
    """Return whether this process belongs to the canonical Production V4 workflow.

    GitHub's GITHUB_* variables describe workflow/job context. They are intentionally
    not treated as proof that the live production phase has started because the same
    variables are inherited by pre-production regression steps in that workflow.
    """
    return (
        str(os.environ.get("GITHUB_ACTIONS") or "").strip().lower() == "true"
        and str(os.environ.get("GITHUB_EVENT_NAME") or "").strip() == "workflow_dispatch"
        and _CANONICAL_WORKFLOW_MARKER in str(os.environ.get("GITHUB_WORKFLOW_REF") or "")
    )


def canonical_runtime_enabled() -> bool:
    """Require both canonical workflow identity and an explicit Isco phase transition."""
    explicit = str(os.environ.get(_RUNTIME_ENV) or "").strip().lower()
    return canonical_workflow_identity() and explicit in _TRUE_VALUES


def activate_canonical_runtime() -> None:
    """Activate live runtime after pre-production certification has completed.

    The transition is application-owned rather than inferred from ambient CI context.
    GITHUB_ENV is updated so later workflow steps inherit the same phase identity, while
    os.environ is updated so the current restore/bootstrap process sees it immediately.
    """
    if not canonical_workflow_identity():
        raise RuntimeError("canonical runtime activation requires Production V4 workflow identity")

    os.environ[_RUNTIME_ENV] = "1"
    github_env = str(os.environ.get("GITHUB_ENV") or "").strip()
    if github_env:
        with Path(github_env).open("a", encoding="utf-8") as handle:
            handle.write(f"{_RUNTIME_ENV}=1\n")
