from __future__ import annotations

import os
import sys

try:
    from scripts import planning_checkpoint_state_core as _core
    from scripts.runtime_phase import canonical_runtime_enabled as _canonical_runtime_enabled
except ModuleNotFoundError:  # direct script/package compatibility
    import planning_checkpoint_state_core as _core
    from runtime_phase import canonical_runtime_enabled as _canonical_runtime_enabled

# Run130 family closure: the historical checkpoint implementation remains intact as
# compatibility/storage logic, but it no longer owns production phase identity. Patch
# its module-global lookup before exporting any function so every internal call uses
# the one application-owned runtime_phase authority.
_core.canonical_runtime_enabled = _canonical_runtime_enabled

# Run135 planning durability closure: bind persisted responses to the canonical
# planning-only execution seam, not to the whole production entrypoint. The seam is
# executable composition (not a hand-maintained dependency allowlist), so every patch
# that can change planning must be activated there and therefore enters the transitive
# contract hash automatically. Voice, visual, Gold, Telegram and release changes no
# longer invalidate otherwise compatible completed planning shards.
_core.PLANNING_CONTRACT_ROOTS = ("scripts/planning_runtime_contract.py",)

_ISOLATED_CHILD_MODE = "isolated_sibling_child_no_cross_run"
_original_install_runtime_persistence_wrapper = _core.install_runtime_persistence_wrapper
_original_persist_runtime_checkpoint = _core.persist_runtime_checkpoint


def _cross_run_checkpoint_enabled() -> bool:
    return (os.environ.get("ISCO_RUNTIME_CHECKPOINT_MODE") or "").strip() != _ISOLATED_CHILD_MODE


def install_runtime_persistence_wrapper(orchestrator_module) -> None:
    """Install durable cross-run persistence unless this is an isolated sibling child.

    Sibling Shorts are already inside one explicitly authorized parent production bundle
    and execute sequentially in fresh subprocesses. They inherit live-runtime authority
    from the parent, but must not inherit the parent's durable planning-state identity:
    the child has a different approved brief/snapshot binding. Trying to persist it under
    the parent's planning-state identity would either fail after successful media work or
    corrupt cross-run resume authority. Child planning remains fully fail-closed in-run;
    only cross-run checkpoint write/resume is disabled for that isolated child process.
    """
    if not _cross_run_checkpoint_enabled():
        print("Durable planning checkpoint disabled for isolated sibling child runtime")
        return
    _original_install_runtime_persistence_wrapper(orchestrator_module)


def persist_runtime_checkpoint(*, repo_root, engine_root, status):
    if not _cross_run_checkpoint_enabled():
        return _core.PersistStatus(True, False, "isolated sibling child has no cross-run checkpoint authority")
    return _original_persist_runtime_checkpoint(
        repo_root=repo_root,
        engine_root=engine_root,
        status=status,
    )


_core.install_runtime_persistence_wrapper = install_runtime_persistence_wrapper
_core.persist_runtime_checkpoint = persist_runtime_checkpoint

for _name in dir(_core):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_core, _name)

canonical_runtime_enabled = _canonical_runtime_enabled
sys.modules[__name__] = _core