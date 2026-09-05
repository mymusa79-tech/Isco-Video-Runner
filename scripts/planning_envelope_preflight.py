from __future__ import annotations

"""Compatibility wrapper: keep the certified preflight implementation byte-identical,
while feeding Call 1b the same Engine-enriched EditorialIntent shape used at runtime.
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
    _base.main()


if __name__ == "__main__":
    main()
