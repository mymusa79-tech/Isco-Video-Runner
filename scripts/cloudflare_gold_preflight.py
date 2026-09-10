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
