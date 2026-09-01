from __future__ import annotations

import math

from scripts import provider_capacity_hardening as capacity


# Provider ceilings are hard admission boundaries, not safe operating targets. Run #154
# exposed the missing distinction: the native Short request was estimated at 7,993
# tokens against an observed 8,000 TPM limit, leaving only seven tokens (0.0875%). A
# tiny tokenizer/provider-accounting delta can turn that mathematically-fitting request
# into a terminal production failure.
#
# Keep the existing conservative request estimate and add a separate operational
# reserve. This does not change any provider limit or quality budget. It only prevents
# the router from deliberately scheduling requests at the cliff edge.
GROQ_OPERATIONAL_HEADROOM_FRACTION = 0.10
GROQ_OPERATIONAL_HEADROOM_MIN_TOKENS = 512


def groq_operational_headroom_tokens(limit: int | None) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return 0
    return max(
        GROQ_OPERATIONAL_HEADROOM_MIN_TOKENS,
        int(math.ceil(limit * GROQ_OPERATIONAL_HEADROOM_FRACTION)),
    )


def apply_operational_headroom(
    model_name: str,
    required_tokens: int,
    base_decision: dict,
) -> dict:
    """Strengthen one canonical Groq decision without changing provider authority."""
    result = dict(base_decision)
    required = max(0, int(required_tokens))
    limit = result.get("actual_limit")
    remaining = result.get("remaining_tokens")
    headroom = groq_operational_headroom_tokens(limit)
    required_with_headroom = required + headroom
    result["operational_headroom_tokens"] = headroom
    result["required_with_operational_headroom"] = required_with_headroom

    # Existing hard failures remain authoritative and retain their exact taxonomy.
    if result.get("action") in {"impossible", "unavailable"}:
        return result
    if not isinstance(limit, int) or limit <= 0:
        return result

    # A request may fit the nominal ceiling yet leave no safe operating reserve. Treat
    # that as non-waitable: a minute-window reset cannot make the hard ceiling larger.
    if required_with_headroom > limit:
        contacted = bool(capacity._model_state(model_name).get("contacted"))
        return {
            **result,
            "action": "impossible",
            "reason": (
                "actual_limit_operational_headroom_below_required"
                if contacted
                else "initial_fallback_operational_headroom_below_required"
            ),
        }

    # Preserve the old remaining<required wait taxonomy when it already applies. Add a
    # distinct wait reason only for the newly protected edge where the request itself
    # fits the current window but would consume the operational reserve.
    if result.get("action") == "wait":
        return result
    if isinstance(remaining, int) and required_with_headroom > remaining:
        return {
            **result,
            "action": "wait",
            "reason": "remaining_below_required_with_operational_headroom",
        }
    return result


def install_operational_headroom_contract() -> None:
    if getattr(capacity, "_ISCO_OPERATIONAL_HEADROOM_CONTRACT", False):
        return
    original = capacity.groq_admission_decision

    def headroom_aware_admission(model_name: str, required_tokens: int) -> dict:
        base = original(model_name, required_tokens)
        return apply_operational_headroom(model_name, required_tokens, base)

    capacity.groq_admission_decision = headroom_aware_admission
    capacity._ISCO_OPERATIONAL_HEADROOM_CONTRACT = True
    print(
        "Operational provider headroom installed: "
        f"groq_fraction={GROQ_OPERATIONAL_HEADROOM_FRACTION:.2f} "
        f"groq_min_tokens={GROQ_OPERATIONAL_HEADROOM_MIN_TOKENS} "
        "provider_limits_unchanged=true"
    )
