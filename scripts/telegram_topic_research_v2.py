from __future__ import annotations

import sys
from pathlib import Path

# GitHub Actions executes this wrapper directly as ``python scripts/...``.
# Restore the repository root before importing package-owned modules.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import telegram_topic_research_v2_core as core


def __getattr__(name: str):
    return getattr(core, name)


def main() -> None:
    core.memory_ui._install_policy()
    from scripts import telegram_canonical_status_bridge as canonical_status_bridge
    from scripts import telegram_creator_control_center_v5 as creator_v5
    from scripts import telegram_persistent_control_ui as persistent_ui
    from scripts import telegram_rich_integration as rich_integration

    persistent_ui.install()
    core.memory_ui._install_library_split()
    core.memory_ui._install_choice_clarity()
    canonical_status_bridge.install()
    rich_integration.install()
    core.active._install()
    core.install_v2()
    # Install last so V5 is a presentation/navigation layer over the fully
    # certified Topic Research V2 + Production authority stack.
    creator_v5.install(core)
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    core.memory_ui._require_poll_identity(mode)
    core.panel.main()


if __name__ == "__main__":
    main()
