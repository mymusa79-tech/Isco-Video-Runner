from __future__ import annotations

import os
from pathlib import Path

from scripts import run_v3_voice as production
from scripts.dynamic_planning_capacity import install_dynamic_planning_capacity
from scripts.immutable_planning_snapshot import bootstrap_immutable_planning_checkpoint


def _install_pre_router_snapshot_bootstrap() -> None:
    original = production.install_provider_capacity_hardening

    def capacity_then_checkpoint() -> None:
        original()
        bootstrap_immutable_planning_checkpoint(
            repo_root=Path(__file__).resolve().parents[1],
            engine_root=Path.cwd().resolve(),
            encryption_key=str(os.environ.get("STATE_ENCRYPTION_KEY") or ""),
        )

    production.install_provider_capacity_hardening = capacity_then_checkpoint


def _install_post_run125_dynamic_capacity() -> None:
    original = production.install_runtime_closure

    def runtime_then_dynamic() -> None:
        original()
        install_dynamic_planning_capacity()

    production.install_runtime_closure = runtime_then_dynamic


def main() -> None:
    _install_pre_router_snapshot_bootstrap()
    _install_post_run125_dynamic_capacity()
    production.main()


if __name__ == "__main__":
    main()
