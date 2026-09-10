from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from scripts.gold_cloudflare_vision_fallback import (
    CloudflareGoldVisionUnavailable,
    _credentials,
    _enabled,
    _prove_model_access,
    _prove_workers_free,
)


@contextmanager
def _canonical_cloudflare_files_from_runner_temp():
    """Expose the already-materialized production secrets to the exact Gold probes.

    The canonical workflow intentionally stores secrets in RUNNER_TEMP files and only
    exports their paths to the production process later. Provider preflight runs before
    that process, so bind the same files temporarily instead of duplicating auth logic.
    """
    runner_temp = str(os.environ.get("RUNNER_TEMP") or "").strip()
    if not runner_temp:
        raise CloudflareGoldVisionUnavailable("Cloudflare preflight runner temp is unavailable")
    root = Path(runner_temp) / "isco-secrets"
    token_file = root / "cloudflare-api-token"
    account_file = root / "cloudflare-account-id"
    previous = {
        "CLOUDFLARE_API_TOKEN_FILE": os.environ.get("CLOUDFLARE_API_TOKEN_FILE"),
        "CLOUDFLARE_ACCOUNT_ID_FILE": os.environ.get("CLOUDFLARE_ACCOUNT_ID_FILE"),
    }
    os.environ["CLOUDFLARE_API_TOKEN_FILE"] = str(token_file)
    os.environ["CLOUDFLARE_ACCOUNT_ID_FILE"] = str(account_file)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def preflight_gold_cloudflare_from_runner_temp() -> None:
    """Prove the exact zero-cost Gold fallback capability before expensive production.

    This deliberately calls the same credential, Billing-Read/zero-cost and exact-model
    access routines used immediately before runtime inference. Any inability to prove
    zero-cost remains fail-closed; no inference, subscription, billing or write request
    is made by this preflight.
    """
    if not _enabled():
        print("Cloudflare Gold Vision preflight: route disabled")
        return
    with _canonical_cloudflare_files_from_runner_temp():
        token, account_id = _credentials()
        _prove_workers_free(token, account_id)
        _prove_model_access(token, account_id)
    print("Cloudflare Gold Vision preflight PASS: exact zero-cost/model capability proven")


def _disable_optional_route_for_following_steps() -> None:
    """Disable only the optional Cloudflare route for the rest of this workflow.

    Updating the current process protects any later in-process caller. Appending the
    same value to GITHUB_ENV carries the decision into subsequent GitHub Actions steps.
    If that persistence channel is unavailable, the runtime adapter still remains
    fail-closed and will never infer without proving zero-cost eligibility itself.
    """
    os.environ["CLOUDFLARE_GOLD_VISION_FREE_ONLY"] = "false"
    github_env = str(os.environ.get("GITHUB_ENV") or "").strip()
    if not github_env:
        return
    try:
        with Path(github_env).open("a", encoding="utf-8") as handle:
            handle.write("CLOUDFLARE_GOLD_VISION_FREE_ONLY=false\n")
    except OSError as exc:
        print(
            "Cloudflare Gold Vision preflight warning: optional disable could not be "
            f"persisted to GITHUB_ENV ({type(exc).__name__}); runtime remains fail-closed"
        )


def preflight_optional_gold_cloudflare_from_runner_temp() -> bool:
    """Probe Cloudflare early without making a fourth-choice provider production-critical.

    Cloudflare is only the Gold-opening fallback after the primary Vision mesh. A
    credential, Billing-Read, free-tier, quota or model-access problem therefore disables
    Cloudflare for this workflow and production continues. Only the provider's explicit
    availability exception is downgraded; unexpected programming errors still propagate.
    """
    if not _enabled():
        print("Cloudflare Gold Vision preflight: optional route already disabled")
        return False
    try:
        preflight_gold_cloudflare_from_runner_temp()
    except CloudflareGoldVisionUnavailable as exc:
        _disable_optional_route_for_following_steps()
        detail = " ".join(str(exc).split())[:500]
        print(
            "Cloudflare Gold Vision preflight OPTIONAL_UNAVAILABLE: "
            f"{detail}; production continues without Cloudflare Gold fallback"
        )
        return False
    return True
