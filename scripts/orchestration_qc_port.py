from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts import final_master_qc as core


PORT_ID = "qc-runtime-port-v1"
PORT_VERSION = 1
STAGE_ID = "qc"
PROVIDER_OWNER = "final-master-qc-core"
RETRY_OWNER = "final-master-qc-core"


def run_final_master_qc(output_dir: Path) -> dict[str, Any]:
    """Execute the certified Final Master QC through one stable stage seam.

    L7.5 owns only this call topology. The enforcing media scan, thresholds,
    report write, blocking semantics, exception type, and zero-retry behavior remain
    entirely inside ``scripts.final_master_qc``.
    """
    return core.run_final_master_qc(output_dir)
