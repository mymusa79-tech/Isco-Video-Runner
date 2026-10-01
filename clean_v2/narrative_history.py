from __future__ import annotations

"""Minimal cross-run memory for narrative variety and Podcast promo excerpt variety.

_select_longform_narrative_profile (clean_v2/pipeline.py) is otherwise pure and
stateless: the same topic always picks the same narrative_format forever, which
is a real source of cross-video sameness for film. This module is the whole of
the fix's persistence surface - a tiny JSON file, read once before Planning and
appended once after it, with no other responsibility. It deliberately does not
reuse scripts/channel_os_memory.py: that module's broader preference/policy
machinery is unused in production today, and this need is narrow enough that a
purpose-built file is simpler to reason about and to keep working.
"""

import json
from pathlib import Path
from typing import Any

from .contracts import atomic_write_json

SCHEMA_VERSION = 1
DEFAULT_MAX_HISTORY = 5

# Film and Short have independent narrative-selection keys; Podcast itself remains a fixed house style.
TRACKED_FORMATS = ("film", "short")
PODCAST_PROMO_HISTORY_KEY = "podcast_promo"
PODCAST_PROMO_MAX_HISTORY = 4


def _read(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {"schema_version": SCHEMA_VERSION}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"schema_version": SCHEMA_VERSION}
    return data if isinstance(data, dict) else {"schema_version": SCHEMA_VERSION}


def recent_narrative_formats(
    path: Path | None,
    fmt: str,
    *,
    limit: int = DEFAULT_MAX_HISTORY,
) -> tuple[str, ...]:
    """Most recent narrative_format values recorded for this format, oldest first."""
    if path is None or fmt not in TRACKED_FORMATS:
        return ()
    values = _read(path).get(fmt)
    if not isinstance(values, list):
        return ()
    return tuple(str(value).strip() for value in values[-limit:] if str(value or "").strip())


def record_narrative_format(
    path: Path | None,
    fmt: str,
    narrative_format: str,
    *,
    limit: int = DEFAULT_MAX_HISTORY,
) -> None:
    """Append one just-selected narrative_format, keeping only the most recent `limit`."""
    narrative_format = str(narrative_format or "").strip()
    if path is None or fmt not in TRACKED_FORMATS or not narrative_format:
        return
    data = _read(path)
    data["schema_version"] = SCHEMA_VERSION
    history = data.get(fmt)
    history = [str(v) for v in history if str(v or "").strip()] if isinstance(history, list) else []
    history.append(narrative_format)
    data[fmt] = history[-limit:]
    atomic_write_json(path, data)



def recent_podcast_promo_signatures(
    path: Path | None,
    *,
    limit: int = PODCAST_PROMO_MAX_HISTORY,
) -> tuple[str, ...]:
    """Recent derived-Podcast promo selection signatures, oldest first.

    This is deliberately separate from the Podcast narrative_format: the episode
    house style remains dialogue_qa; only the locally selected derived-Short
    excerpt pattern gets cross-run memory.
    """
    if path is None:
        return ()
    values = _read(path).get(PODCAST_PROMO_HISTORY_KEY)
    if not isinstance(values, list):
        return ()
    return tuple(str(value).strip() for value in values[-limit:] if str(value or "").strip())


def record_podcast_promo_signature(
    path: Path | None,
    signature: str,
    *,
    limit: int = PODCAST_PROMO_MAX_HISTORY,
) -> None:
    """Append one selected derived-Podcast promo pattern signature."""
    signature = str(signature or "").strip()
    if path is None or not signature:
        return
    data = _read(path)
    data["schema_version"] = SCHEMA_VERSION
    history = data.get(PODCAST_PROMO_HISTORY_KEY)
    history = [str(v) for v in history if str(v or "").strip()] if isinstance(history, list) else []
    history.append(signature)
    data[PODCAST_PROMO_HISTORY_KEY] = history[-limit:]
    atomic_write_json(path, data)
