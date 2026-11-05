"""Opt-in Cairo font preflight for approved Cinematic Monoclip Telegram runs.

Only explicitly approved Nidaa Shorts with visual_style set can trigger this
download. No other format's font installation or rendering is touched. Runs
before the paid TTS pipeline so a missing font cannot waste voice quota.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Mapping

from clean_v2.short_timed_text import ShortTimedTextError, _require_monoclip_cairo_font

CAIRO_URL = (
    "https://raw.githubusercontent.com/google/fonts/main/ofl/cairo/"
    "Cairo%5Bslnt%2Cwght%5D.ttf"
)
_MIN_FONT_BYTES = 100_000
_MAX_FONT_BYTES = 3_000_000


def requires_cinematic_cairo(request: Mapping[str, Any]) -> bool:
    pack = request.get("owner_pack")
    if not isinstance(pack, Mapping):
        return False
    doc = pack.get("production")
    return (
        isinstance(doc, Mapping)
        and doc.get("format") == "short"
        and doc.get("visual_style") in {"color_cinematic", "bw_cinematic"}
    )


def prepare_cinematic_cairo(request: Mapping[str, Any]) -> str:
    """Return 'not_applicable', 'installed' or 'prepared'; otherwise fail closed."""
    if not requires_cinematic_cairo(request):
        return "not_applicable"
    try:
        _require_monoclip_cairo_font()
        return "installed"
    except ShortTimedTextError as exc:
        if str(exc) != "monoclip_cairo_bold_font_not_installed":
            raise

    directory = Path.home() / ".local" / "share" / "fonts" / "isco-cairo"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "Cairo.ttf"
    temporary = directory / ".Cairo.download.ttf"
    try:
        subprocess.run(
            ["curl", "-fLSsS", "--retry", "2", "--connect-timeout", "15",
             "--max-time", "90", CAIRO_URL, "-o", str(temporary)],
            check=True, timeout=115,
        )
        size = temporary.stat().st_size
        if not _MIN_FONT_BYTES <= size <= _MAX_FONT_BYTES:
            raise RuntimeError("monoclip_cairo_download_size_invalid")
        scan = subprocess.run(
            ["fc-scan", "-f", "%{family}", str(temporary)],
            check=True, capture_output=True, text=True, timeout=15,
        )
        if "cairo" not in scan.stdout.casefold():
            raise RuntimeError("monoclip_cairo_download_family_invalid")
        os.replace(temporary, target)
        subprocess.run(
            ["fc-cache", "-f", str(directory)], check=True, timeout=45,
        )
        _require_monoclip_cairo_font()
        return "prepared"
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("monoclip_cairo_preflight_unavailable") from exc
    finally:
        temporary.unlink(missing_ok=True)
