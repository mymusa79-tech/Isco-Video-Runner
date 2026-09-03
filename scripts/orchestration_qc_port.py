from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts import final_master_qc as core
from scripts.final_master_acceptance_v2 import require_final_master_acceptance


PORT_ID = "qc-runtime-port-v1"
PORT_VERSION = 2
STAGE_ID = "qc"
PROVIDER_OWNER = "final-master-qc-core"
RETRY_OWNER = "final-master-qc-core"


def run_final_master_qc(output_dir: Path) -> dict[str, Any]:
    """Execute P4 and prove its PASS receipt belongs to the exact current artifacts."""
    report = core.run_final_master_qc(output_dir)
    # The stable port is the canonical P4->P5 boundary. Do not return a successful QC
    # result until the self-contained final.master.acceptance.v2 receipt revalidates
    # final.mp4 + plan + quality + timeline against the current bytes.
    return require_final_master_acceptance(Path(output_dir), report=report)
