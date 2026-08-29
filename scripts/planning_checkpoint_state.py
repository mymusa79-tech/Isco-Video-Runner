from __future__ import annotations

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

for _name in dir(_core):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_core, _name)

canonical_runtime_enabled = _canonical_runtime_enabled
sys.modules[__name__] = _core