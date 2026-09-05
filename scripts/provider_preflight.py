from __future__ import annotations

import sys

from scripts import provider_preflight_core as _core
from scripts.youtube_oauth_readonly_firewall import enforce_from_runner_temp


_original_main = _core.main


def main() -> None:
    # Canonical production materializes YouTube OAuth immediately before this preflight.
    # Certify the effective Google grant is read-only before any Engine process receives it.
    enforce_from_runner_temp()
    _original_main()


# Preserve provider_preflight's long-standing import API for all existing tests/callers.
# Imported callers receive the original implementation module with only main() hardened;
# direct execution (the production workflow path) runs the hardened main below.
_core.main = main

if __name__ == "__main__":
    main()
else:
    sys.modules[__name__] = _core
