from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_RUN_SPECIFIC = re.compile(r"^run\d+[-_.]", re.I)
_USES = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)", re.M)
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$", re.I)


@dataclass(frozen=True)
class WorkflowIssue:
    path: str
    code: str
    detail: str


def audit_workflows(root: Path) -> list[WorkflowIssue]:
    issues: list[WorkflowIssue] = []
    workflow_dir = root / ".github" / "workflows"
    if not workflow_dir.is_dir():
        return [WorkflowIssue(str(workflow_dir), "workflow_dir_missing", "workflow directory missing")]

    for path in sorted(workflow_dir.glob("*.y*ml")):
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        if _RUN_SPECIFIC.match(path.name):
            issues.append(
                WorkflowIssue(
                    rel,
                    "run_specific_workflow_forbidden",
                    "historical run-number workflows must be deleted or converted to a permanent generic verifier",
                )
            )
        if "permissions:" not in text:
            issues.append(
                WorkflowIssue(rel, "permissions_missing", "workflow must declare explicit least-privilege permissions")
            )
        for match in _USES.finditer(text):
            target = match.group(1).strip("'\"")
            if target.startswith("./") or target.startswith("docker://"):
                continue
            if "@" not in target:
                issues.append(WorkflowIssue(rel, "action_ref_missing", target))
                continue
            action, ref = target.rsplit("@", 1)
            if not _FULL_SHA.fullmatch(ref):
                issues.append(
                    WorkflowIssue(
                        rel,
                        "action_not_pinned_to_full_sha",
                        f"{action}@{ref}",
                    )
                )
    return issues


def assert_workflow_hygiene(root: Path) -> None:
    issues = audit_workflows(root)
    if issues:
        detail = " | ".join(f"{item.path}:{item.code}:{item.detail}" for item in issues)
        raise RuntimeError("Workflow hygiene gate failed: " + detail)
