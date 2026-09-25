from __future__ import annotations

"""Optional, free-only Cloudflare image anchors for Clean V2."""

import base64
import binascii
import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping


CLOUDFLARE_IMAGE_MODEL = "@cf/black-forest-labs/flux-2-klein-4b"
CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"
CLOUDFLARE_IMAGE_MAX_BYTES = 24 * 1024 * 1024
CLOUDFLARE_IMAGE_TIMEOUT_SECONDS = 150
CLOUDFLARE_PROBE_TIMEOUT_SECONDS = 15


class CloudflareAIStillUnavailable(RuntimeError):
    """The optional free-only AI still route is unavailable."""


def _read_secret(name: str) -> str:
    direct = str(os.environ.get(name) or "").strip()
    if direct:
        return direct
    raw_path = str(os.environ.get(f"{name}_FILE") or "").strip()
    if not raw_path:
        return ""
    try:
        path = Path(raw_path)
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    except (OSError, UnicodeError):
        return ""


def _enabled() -> bool:
    return (
        str(os.environ.get("CLEAN_V2_AI_STILL_FREE_ONLY") or "").strip().lower()
        == "true"
    )


def _credentials() -> tuple[str, str]:
    token = _read_secret("CLOUDFLARE_API_TOKEN")
    account_id = _read_secret("CLOUDFLARE_ACCOUNT_ID")
    if not token or not account_id:
        raise CloudflareAIStillUnavailable("cloudflare_image_credentials_unavailable")
    if len(account_id) != 32 or any(
        character not in "0123456789abcdefABCDEF" for character in account_id
    ):
        raise CloudflareAIStillUnavailable("cloudflare_image_account_id_malformed")
    return token, account_id


def _error_detail(body: object) -> str:
    if not isinstance(body, Mapping):
        return "unknown_error"
    errors = body.get("errors")
    if isinstance(errors, list):
        details = []
        for item in errors[:3]:
            if not isinstance(item, Mapping):
                continue
            code = str(item.get("code") or "").strip()
            message = " ".join(str(item.get("message") or "").split())[:160]
            details.append(f"{code}:{message}" if code else message)
        if details:
            return " | ".join(details)
    return " ".join(str(body.get("message") or "unknown_error").split())[:180]


def _request_json(
    request: urllib.request.Request,
    *,
    stage: str,
    timeout: int,
) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(CLOUDFLARE_IMAGE_MAX_BYTES + 1)
            status = int(getattr(response, "status", 200))
    except urllib.error.HTTPError as exc:
        raw = exc.read(CLOUDFLARE_IMAGE_MAX_BYTES + 1)
        status = exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CloudflareAIStillUnavailable(
            f"cloudflare_image_{stage}_transport:{type(exc).__name__}"
        ) from exc
    if len(raw) > CLOUDFLARE_IMAGE_MAX_BYTES:
        raise CloudflareAIStillUnavailable(f"cloudflare_image_{stage}_response_too_large")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CloudflareAIStillUnavailable(
            f"cloudflare_image_{stage}_response_not_json"
        ) from exc
    if not isinstance(body, dict):
        raise CloudflareAIStillUnavailable(
            f"cloudflare_image_{stage}_response_not_object"
        )
    if status >= 400 or body.get("success") is not True:
        raise CloudflareAIStillUnavailable(
            f"cloudflare_image_{stage}_http_{status}:"
            + _error_detail(body)
        )
    return body


def _subscription_is_active(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    return str(value.get("state") or "").strip().lower() not in {
        "cancelled",
        "canceled",
        "expired",
        "failed",
    }


def _subscription_is_proven_free(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    if not _subscription_is_active(value):
        return True
    price = value.get("price")
    try:
        if price is not None and float(price) != 0.0:
            return False
    except (TypeError, ValueError):
        return False
    rate_plan = value.get("rate_plan")
    return isinstance(rate_plan, Mapping) and str(
        rate_plan.get("id") or ""
    ).strip().lower() in {"free", "partners_free"}


def _prove_workers_free(token: str, account_id: str) -> None:
    body = _request_json(
        urllib.request.Request(
            f"{CLOUDFLARE_API_BASE}/accounts/{account_id}/subscriptions?per_page=50",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        ),
        stage="preflight",
        timeout=CLOUDFLARE_PROBE_TIMEOUT_SECONDS,
    )
    subscriptions = body.get("result")
    if not isinstance(subscriptions, list):
        raise CloudflareAIStillUnavailable(
            "cloudflare_image_zero_cost_proof_unavailable"
        )
    if any(
        _subscription_is_active(item) and not _subscription_is_proven_free(item)
        for item in subscriptions
    ):
        raise CloudflareAIStillUnavailable(
            "cloudflare_image_paid_or_unknown_subscription_detected"
        )


def _image_media_type(raw: bytes) -> str:
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    raise CloudflareAIStillUnavailable("cloudflare_image_payload_type_invalid")


def _decode_image(body: Mapping[str, Any]) -> bytes:
    result = body.get("result")
    encoded = result.get("image") if isinstance(result, Mapping) else None
    if not isinstance(encoded, str) or not encoded:
        raise CloudflareAIStillUnavailable("cloudflare_image_payload_missing")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise CloudflareAIStillUnavailable("cloudflare_image_payload_invalid") from exc
    if not 1024 <= len(raw) <= CLOUDFLARE_IMAGE_MAX_BYTES:
        raise CloudflareAIStillUnavailable("cloudflare_image_payload_size_invalid")
    _image_media_type(raw)
    return raw


def _reference_part(reference: Path) -> tuple[str, bytes, str]:
    try:
        raw = reference.read_bytes()
    except OSError as exc:
        raise CloudflareAIStillUnavailable(
            "cloudflare_image_reference_unreadable"
        ) from exc
    if not 1024 <= len(raw) <= 8 * 1024 * 1024:
        raise CloudflareAIStillUnavailable("cloudflare_image_reference_size_invalid")
    return reference.name, raw, _image_media_type(raw)


def _multipart_body(
    fields: Mapping[str, str],
    *,
    reference: Path | None,
) -> tuple[bytes, str]:
    boundary = "isco-clean-v2-" + hashlib.sha256(
        json.dumps(dict(fields), sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                    "ascii"
                ),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    if reference is not None:
        name, raw, content_type = _reference_part(reference)
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                (
                    'Content-Disposition: form-data; name="input_image_0"; '
                    f'filename="{name}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
                raw,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def generate_cloudflare_ai_still(
    *,
    prompt: str,
    destination: Path,
    fmt: str,
    reference: Path | None = None,
) -> dict[str, Any]:
    """Generate one anchor; every failure is handled by the caller as stock fallback."""
    if not _enabled():
        raise CloudflareAIStillUnavailable("cloudflare_image_feature_flag_disabled")
    compact_prompt = " ".join(str(prompt or "").split()).strip()
    if not compact_prompt or len(compact_prompt) > 2048:
        raise CloudflareAIStillUnavailable("cloudflare_image_prompt_invalid")
    if fmt == "short":
        width, height = 720, 1280
    elif fmt == "film":
        width, height = 1280, 720
    else:
        raise CloudflareAIStillUnavailable("cloudflare_image_format_unsupported")

    token, account_id = _credentials()
    _prove_workers_free(token, account_id)
    prompt_hash = hashlib.sha256(compact_prompt.encode("utf-8")).hexdigest()
    seed = int(prompt_hash[:8], 16) & 0x7FFFFFFF
    payload, content_type = _multipart_body(
        {
            "prompt": compact_prompt,
            "width": str(width),
            "height": str(height),
            "seed": str(seed),
        },
        reference=Path(reference) if reference is not None else None,
    )
    body = _request_json(
        urllib.request.Request(
            f"{CLOUDFLARE_API_BASE}/accounts/{account_id}/ai/run/"
            f"{CLOUDFLARE_IMAGE_MODEL}",
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": content_type,
            },
        ),
        stage="inference",
        timeout=CLOUDFLARE_IMAGE_TIMEOUT_SECONDS,
    )
    raw_image = _decode_image(body)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw_image)
    return {
        "provider": "cloudflare_workers_ai",
        "model": CLOUDFLARE_IMAGE_MODEL,
        "seed": seed,
        "width": width,
        "height": height,
        "content_type": _image_media_type(raw_image),
        "prompt_sha256": prompt_hash,
        "reference_used": reference is not None,
        "ai_generated": True,
        "source_url": (
            "https://developers.cloudflare.com/workers-ai/models/flux-2-klein-4b/"
        ),
    }
