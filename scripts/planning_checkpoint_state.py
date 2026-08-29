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

for _name in dir(_core):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_core, _name)

canonical_runtime_enabled = _canonical_runtime_enabled
sys.modules[__name__] = _core
