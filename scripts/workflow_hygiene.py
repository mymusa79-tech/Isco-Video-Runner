from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_RUN_SPECIFIC = re.compile(r"^run\d+[-_.]", re.I)
_RETIRED_ONE_TIME_PATTERNS = (
    re.compile(r"^migrate-.*(?:production|memory).*\.ya?ml$", re.I),
    re.compile(r"^youtube-analytics-backfill-write-once(?:-v\d+)?\.ya?ml$", re.I),
    re.compile(r"^local-brain-smoke\.ya?ml$", re.I),
)
_RETIRED_EXACT_WORKFLOWS = frozenset({"p0c-migration-contracts.yml"})
_USES = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)", re.M)
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$", re.I)
_ENGINE_ENV = re.compile(r"^\s*(?:ISCO_ENGINE_SHA|ENGINE_SHA|EXPECTED_ENGINE_SHA):\s*([0-9a-f]{40})\s*$", re.M)
_ENGINE_CHECKOUT = re.compile(
    r"repository:\s*mymusa79-tech/Isco-Video-Agent\s*\n\s*ref:\s*([0-9a-f]{40})",
    re.M,
)
_CANONICAL_PIN_WORKFLOWS = (
    "produce-resilient-v4.yml",
    "telegram-editorial-control.yml",
    "telegram-production-request.yml",
    "verify-human-editorial-intent-m7.yml",
    "verify-m11-live-integration.yml",
)


@dataclass(frozen=True)
class WorkflowIssue:
    path: str
    code: str
    detail: str


def _canonical_engine_pin(workflow_dir: Path) -> str | None:
    production = workflow_dir / "produce-resilient-v4.yml"
    if not production.is_file():
        return None
    text = production.read_text(encoding="utf-8")
    env_pins = re.findall(r"^\s*ISCO_ENGINE_SHA:\s*([0-9a-f]{40})\s*$", text, flags=re.M)
    checkout_pins = _ENGINE_CHECKOUT.findall(text)
    if len(env_pins) != 1 or len(checkout_pins) != 1 or env_pins[0] != checkout_pins[0]:
        return None
    return env_pins[0]


def _retired_one_time_workflow(name: str) -> bool:
    return name in _RETIRED_EXACT_WORKFLOWS or any(pattern.match(name) for pattern in _RETIRED_ONE_TIME_PATTERNS)


def audit_workflows(root: Path) -> list[WorkflowIssue]:
    issues: list[WorkflowIssue] = []
    workflow_dir = root / ".github" / "workflows"
    if not workflow_dir.is_dir():
        return [WorkflowIssue(str(workflow_dir), "workflow_dir_missing", "workflow directory missing")]

    canonical_pin = _canonical_engine_pin(workflow_dir)
    if canonical_pin is None:
        issues.append(
            WorkflowIssue(
                ".github/workflows/produce-resilient-v4.yml",
                "canonical_engine_pin_invalid",
                "production checkout ref and ISCO_ENGINE_SHA must be one identical full SHA",
            )
        )

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
        if _retired_one_time_workflow(path.name):
            issues.append(
                WorkflowIssue(
                    rel,
                    "retired_one_time_workflow_forbidden",
                    "completed migration/write-once workflow must stay retired; use a reviewed permanent diagnostic instead",
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

        if canonical_pin and path.name in _CANONICAL_PIN_WORKFLOWS:
            pins = _ENGINE_ENV.findall(text)
            if path.name == "produce-resilient-v4.yml":
                pins += _ENGINE_CHECKOUT.findall(text)
            if not pins or any(pin != canonical_pin for pin in pins):
                issues.append(
                    WorkflowIssue(
                        rel,
                        "engine_pin_drift",
                        f"expected only canonical Engine SHA {canonical_pin}, found {pins}",
                    )
                )
    return issues


def assert_workflow_hygiene(root: Path) -> None:
    issues = audit_workflows(root)
    if issues:
        detail = " | ".join(f"{item.path}:{item.code}:{item.detail}" for item in issues)
        raise RuntimeError("Workflow hygiene gate failed: " + detail)
