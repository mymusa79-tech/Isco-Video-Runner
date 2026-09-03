from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts import final_master_qc as core
from scripts.final_master_acceptance_v2 import (
    require_final_master_acceptance,
    seal_final_master_acceptance,
)


PORT_ID = "qc-runtime-port-v1"
PORT_VERSION = 2
STAGE_ID = "qc"
PROVIDER_OWNER = "final-master-qc-core"
RETRY_OWNER = "final-master-qc-core"


def run_final_master_qc(output_dir: Path) -> dict[str, Any]:
    """Run the certified QC core once, then seal/revalidate the F24 P4 receipt."""
    root = Path(output_dir)
    report = core.run_final_master_qc(root)
    seal_final_master_acceptance(root, report)
    return require_final_master_acceptance(root)
