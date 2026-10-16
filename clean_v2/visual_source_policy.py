"""Per-series visual source policy (zero provider calls, pure decisions).

Order of sources is an editorial decision of the channel, not of whichever model
planned the beat:

* Nidaa Al-Yaqatha Short and long film: real stock video first, then stock photos,
  and a free AI still only as a bounded fallback when no stock candidate is admitted.
* Kharij Al-Nass podcast: stock or AI per the editorial plan; nothing is forced.
* Ala Mahl: approved AI images are the base. It never searches stock by default
  (its ZIP assets are served by ``owner_art`` and never reach the stock layer).

Approved owner/ZIP assets are never subject to this policy.
"""

from __future__ import annotations

from typing import Any, Mapping

STOCK_FIRST = "stock_first_ai_fallback"
EDITORIAL = "editorial_stock_or_ai"
APPROVED_AI_ONLY = "approved_ai_images_only"

# Free AI stills a stock-first run may add after stock failed (same budget as the
# planner's own cap in pipeline._bound_ai_still_preferences).
MAX_AI_FALLBACKS = 2


def source_policy(fmt: str, template: str = "") -> str:
    if str(template or "") == "ala_mahl":
        return APPROVED_AI_ONLY
    if str(fmt or "") in {"short", "film"}:
        return STOCK_FIRST
    return EDITORIAL


def effective_preference(beat: Mapping[str, Any], fmt: str, template: str = "") -> str:
    """Source preference the acquisition layer must try FIRST for this beat."""
    preference = str(beat.get("source_preference") or "stock_motion")
    if beat.get("owner_asset"):
        return preference  # approved assets are served elsewhere, never re-sourced
    if source_policy(fmt, template) == STOCK_FIRST and preference in {"ai_still", "stock_still"}:
        # Video is searched first; the existing third step still tries stock photos.
        return "stock_motion"
    return preference


def ai_fallback_allowed(beat: Mapping[str, Any], fmt: str, template: str = "", used: int = 0) -> bool:
    return (
        source_policy(fmt, template) == STOCK_FIRST
        and not beat.get("owner_asset")
        and not beat.get("_ai_fallback_pass")
        and used < MAX_AI_FALLBACKS
    )
