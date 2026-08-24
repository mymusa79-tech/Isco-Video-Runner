from __future__ import annotations

import os


# Production-only runtime binding for the Run #92 opening feasibility fix.
# Ordinary unit/regression imports remain side-effect free. The V4 production step
# already exports all three markers before Python starts; no workflow/secret change
# is required and no Production dispatch is performed here.
if (
    os.environ.get("GITHUB_ACTIONS") == "true"
    and os.environ.get("REQUEST_FILE")
    and os.environ.get("ISCO_ENGINE_SHA")
):
    from scripts.opening_feasibility_guard import install_opening_feasibility_guard

    install_opening_feasibility_guard()
