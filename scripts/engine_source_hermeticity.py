from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EngineSourceStatus:
    applicable: bool
    clean: bool
    phase: str
    changed_paths: tuple[str, ...]


def _engine_root() -> Path | None:
    workspace = str(os.environ.get("GITHUB_WORKSPACE") or "").strip()
    if not workspace:
        return None
    candidate = Path(workspace).resolve() / "engine"
    return candidate if (candidate / ".git").exists() else None


def tracked_engine_changes(engine_root: Path) -> tuple[str, ...]:
    """Return tracked Engine paths modified relative to the exact checked-out commit.

    Untracked test outputs are deliberately ignored. A test may create temporary output,
    but it must never rewrite/delete a tracked source, policy, production input, fixture,
    or state file that a later production phase could consume from the shared workspace.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=no"],
        cwd=str(engine_root.resolve()),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("could not inspect Engine tracked source hermeticity")

    changed: list[str] = []
    for raw in result.stdout.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        # Porcelain v1 uses two status columns, one space, then the path. For renames
        # keep the complete path expression; the diagnostic is evidence, not a parser.
        changed.append(line[3:] if len(line) > 3 else line)
    return tuple(changed)


def certify_engine_source_hermeticity(phase: str) -> EngineSourceStatus:
    """Fail closed in GitHub CI when a prior test suite polluted tracked Engine files."""
    label = str(phase or "unknown").strip() or "unknown"
    root = _engine_root()
    if root is None:
        return EngineSourceStatus(False, True, label, ())

    changed = tracked_engine_changes(root)
    if changed:
        joined = ", ".join(changed[:20])
        suffix = "" if len(changed) <= 20 else f" (+{len(changed) - 20} more)"
        raise RuntimeError(
            "ENGINE_SOURCE_NON_HERMETIC "
            f"phase={label} changed={joined}{suffix}"
        )

    print(f"Engine tracked-source hermeticity PASS: phase={label}")
    return EngineSourceStatus(True, True, label, ())
