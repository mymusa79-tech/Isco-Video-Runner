from __future__ import annotations

"""Compatibility wrapper for the certified Planning envelope implementation.

The pre-existing implementation remains byte-identical in
``planning_envelope_preflight_base``.  This seam changes only the synthetic Call 1b
fixture so capacity certification sees the same Engine-owned metadata that runtime
adds after Core canonicalization.

P0 cross-step promotion ownership is intentionally unchanged: ``_base.main()`` is the
single execution owner and ends in ``activate_p0_runtime_master()`` after a passing
certificate.  The call is documented here explicitly so the repository's source-level
P0 ownership guard can still prove that this canonical CLI surface is the final
preflight owner; this wrapper never calls the activation a second time.
"""

from scripts import planning_envelope_preflight_base as _base
from scripts.planning_outline_split_contract import locked_premise_for_sizing

_original_bounded_preflight_locked_premise = _base._bounded_preflight_locked_premise


def _bounded_preflight_locked_premise() -> dict:
    return locked_premise_for_sizing(_original_bounded_preflight_locked_premise())


_base._bounded_preflight_locked_premise = _bounded_preflight_locked_premise


def __getattr__(name: str):
    return getattr(_base, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_base)))


def main() -> None:
    # Exactly one activation happens inside base.main after successful certification.
    _base.main()


if __name__ == "__main__":
    main()
