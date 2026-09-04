from __future__ import annotations

import os
import shutil
from pathlib import Path

from scripts.final_master_format_router import install_format_aware_qc_router


def _remove_link_or_path(path: Path) -> None:
    try:
        if path.is_symlink():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def sanitize_final_observer_cache_before_runtime() -> bool:
    """Install post-planning Final-QC routing, then validate durable cache parents.

    Runtime closure deliberately reaches this module only after planning composition is
    complete. Installing the format router here keeps Final Master QC outside the
    planning checkpoint source closure while placing it immediately before the durable
    Final-QC wrapper captures the live production QC port.

    GitHub Actions restores the shared cache before production starts. Older durable
    layers already validate their own entries; this preflight specifically prevents the
    Final-QC/Observer namespace from traversing a symlinked shared root or namespace
    parent. A rejected shape becomes a clean cache miss and never changes production.
    """
    # This is mandatory even when no shared cache is configured: routing is correctness,
    # durability is only an optimization.
    install_format_aware_qc_router()

    raw = (os.environ.get("ISCO_TTS_CACHE_PATH") or "").strip()
    if not raw:
        return False
    root = Path(raw)
    try:
        if root.exists() and (root.is_symlink() or not root.is_dir()):
            _remove_link_or_path(root)
            root.mkdir(parents=True, exist_ok=True)
            print("Final QC/Observer cache trust preflight: shared root rejected; clean miss")
            return False
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    trusted = True
    observers = root / "observers"
    for path in (root / "final-qc", observers):
        try:
            if path.exists() and (path.is_symlink() or not path.is_dir()):
                _remove_link_or_path(path)
                trusted = False
        except OSError:
            _remove_link_or_path(path)
            trusted = False
    if not trusted:
        print("Final QC/Observer cache trust preflight: namespace parent rejected; clean miss")
    return trusted
