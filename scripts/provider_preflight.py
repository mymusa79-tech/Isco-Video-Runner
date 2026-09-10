from __future__ import annotations

import sys
from pathlib import Path

# Production historically invoked this facade as a file.  Keep that entrypoint
# compatible even though the implementation now lives in sibling package modules.
# The canonical workflow uses ``python -m`` below, but this bootstrap prevents a
# future direct caller from recreating Run 202's package-resolution failure.
if __package__ in {None, ""}:
    _REPO_ROOT = Path(__file__).resolve().parent.parent
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))

from scripts import provider_preflight_core as _core
from scripts.youtube_oauth_readonly_firewall import enforce_from_runner_temp


_original_main = _core.main


def main() -> None:
    # Canonical production materializes YouTube OAuth immediately before this preflight.
    # Certify the effective Google grant is read-only before any Engine process receives it.
    enforce_from_runner_temp()
    _original_main()
    # Import only after the core CLI has parsed/validated its own request. This preserves
    # Runner-only `--help` and unit-test entrypoints where the private Engine is
    # intentionally absent, while production has already installed the pinned Engine.
    from scripts.cloudflare_gold_preflight import (
        preflight_optional_gold_cloudflare_from_runner_temp,
    )

    # Cloudflare is only the fourth-choice Gold-opening Vision fallback. Probe it early
    # so a known-unavailable route is disabled before expensive production, but do not
    # turn an optional provider into a prerequisite for Planning/render/Final Master.
    preflight_optional_gold_cloudflare_from_runner_temp()


# Preserve provider_preflight's long-standing import API for all existing tests/callers.
# Imported callers receive the original implementation module with only main() hardened;
# direct execution (the production workflow path) runs the hardened main below.
_core.main = main

if __name__ == "__main__":
    main()
else:
    sys.modules[__name__] = _core
