from __future__ import annotations

import os

# Production-only bootstrap.  Pre-production certification/tests import many scripts,
# but the dossier transport must be installed only in the real V4 production process,
# where the workflow supplies both the immutable Engine pin and REQUEST_FILE.
if os.environ.get("REQUEST_FILE") and os.environ.get("ISCO_ENGINE_SHA"):
    from .run120_dossier_repair_hardening import install_run120_dossier_repair_hardening

    install_run120_dossier_repair_hardening()
