from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ingress = ROOT / "scripts" / "telegram_v4_ingress.py"
    snapshot = ROOT / "scripts" / "immutable_planning_snapshot.py"

    # Syntax/import bootstrap regression: terminal reconciliation executes the file
    # directly from GitHub Actions, so the Runner root must be installed on sys.path
    # before any `scripts.*` import is evaluated.
    ingress_text = ingress.read_text(encoding="utf-8")
    ast.parse(ingress_text, filename=str(ingress))
    bootstrap = ingress_text.index("sys.path.insert")
    queue_import = ingress_text.index("from scripts.telegram_production_queue import")
    if bootstrap > queue_import:
        raise SystemExit("Telegram V4 direct-execution import bootstrap occurs too late")

    # Runtime-brief regression: Telegram's approved brief must be snapshotted outside
    # the Engine checkout and the tracked compatibility fixture restored to HEAD before
    # provider work. The strict hermeticity gate must not gain an exclusion for it.
    snapshot_text = snapshot.read_text(encoding="utf-8")
    ast.parse(snapshot_text, filename=str(snapshot))
    required = (
        "_telegram_runtime_approved_brief_bytes",
        "verify_brief_approval",
        "git\", \"checkout\", \"--\", _COMMITTED_BRIEF_PATH",
        "engine_worktree_restored=true",
        "production_source=runtime_snapshot",
    )
    missing = [item for item in required if item not in snapshot_text]
    if missing:
        raise SystemExit(f"Telegram runtime brief closure is incomplete: {missing}")

    hermeticity = (ROOT / "scripts" / "engine_source_hermeticity.py").read_text(encoding="utf-8")
    if "approved_brief.json" in hermeticity:
        raise SystemExit("Engine hermeticity was weakened with an approved-brief exception")

    print("Telegram V4 runtime brief + terminal reconciliation contract PASS")


if __name__ == "__main__":
    main()
