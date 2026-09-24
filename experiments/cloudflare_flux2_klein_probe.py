#!/usr/bin/env python3
"""
Isolated Cloudflare Workers AI FLUX.2 Klein 4B probe.

No production imports. No pipeline integration.
Writes five generated images plus a machine-readable manifest under:
  artifacts/cloudflare-flux2-klein-4b/
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

MODEL = "@cf/black-forest-labs/flux-2-klein-4b"
WIDTH = 1024
HEIGHT = 1536
OUTPUT_TILE_NEURONS = 26.05  # Cloudflare published unit rate, Sep 2026.

STYLE = (
    "Photorealistic cinematic still, warm morning lighting, neutral tones leaning golden, "
    "soft contrast, natural-lens perspective, respectful everyday Arab/global details, "
    "no clearly visible faces, no text, no typography, no logos. "
    "Keep the same coherent visual language across a single film series."
)

SUBJECTS = [
    ("01-morning-desk", "A quiet morning work desk in a simple modern room, tidy but lived-in."),
    ("02-coffee-cup", "A natural ceramic coffee cup on a wooden table in the early morning."),
    ("03-notebook", "An open notebook with a pen on a calm everyday workspace."),
    ("04-window-light", "A simple window with soft natural morning sunlight entering through a light curtain."),
    ("05-calm-road", "A calm quiet road in the early morning, subtle everyday surroundings, no prominent people."),
]

ROOT = Path("artifacts/cloudflare-flux2-klein-4b")
ROOT.mkdir(parents=True, exist_ok=True)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def redact_image_payload(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key.lower() == "image" and isinstance(value, str):
                out[key] = f"<base64 omitted: {len(value)} chars>"
            else:
                out[key] = redact_image_payload(value)
        return out
    if isinstance(obj, list):
        return [redact_image_payload(x) for x in obj]
    return obj


def collect_usage_like(obj: Any, path: str = "$") -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}"
            key_l = key.lower()
            if any(term in key_l for term in ("usage", "neuron", "cost", "billing", "unit")):
                found.append({"path": child, "value": redact_image_payload(value)})
            found.extend(collect_usage_like(value, child))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            found.extend(collect_usage_like(value, f"{path}[{i}]"))
    return found


def detect_image_type(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    raise ValueError(f"unknown image signature: {data[:16]!r}")


def output_tiles(width: int, height: int) -> int:
    return math.ceil(width / 512) * math.ceil(height / 512)


def main() -> int:
    token = require_env("CLOUDFLARE_API_TOKEN")
    account = require_env("CLOUDFLARE_ACCOUNT_ID")

    endpoint = (
        f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/"
        "@cf/black-forest-labs/flux-2-klein-4b"
    )

    tile_count = output_tiles(WIDTH, HEIGHT)
    derived_neurons = tile_count * OUTPUT_TILE_NEURONS

    manifest: dict[str, Any] = {
        "experiment": "cloudflare-flux2-klein-4b-isolated",
        "model": MODEL,
        "dimensions": {"width": WIDTH, "height": HEIGHT},
        "style_prompt": STYLE,
        "pricing_basis": {
            "source": "Cloudflare Workers AI published pricing",
            "output_512x512_tiles": tile_count,
            "neurons_per_output_512x512_tile": OUTPUT_TILE_NEURONS,
            "derived_neurons_per_image": round(derived_neurons, 2),
            "note": "Derived from published unit pricing; only API-returned usage is treated as actual telemetry.",
        },
        "images": [],
    }

    print(f"MODEL={MODEL}")
    print(f"SIZE={WIDTH}x{HEIGHT}")
    print(f"PUBLISHED_DERIVED_NEURONS_PER_IMAGE={derived_neurons:.2f}")

    for index, (slug, subject) in enumerate(SUBJECTS, start=1):
        prompt = f"{STYLE} Subject: {subject}"
        response_file = ROOT / f"{slug}.response.json"
        headers_file = ROOT / f"{slug}.headers.txt"

        cmd = [
            "curl",
            "--silent",
            "--show-error",
            "--location",
            "--dump-header",
            str(headers_file),
            "--output",
            str(response_file),
            "--write-out",
            "%{http_code}",
            "--request",
            "POST",
            "--url",
            endpoint,
            "--header",
            f"Authorization: Bearer {token}",
            "--form-string",
            f"prompt={prompt}",
            "--form-string",
            f"width={WIDTH}",
            "--form-string",
            f"height={HEIGHT}",
        ]

        started = time.monotonic()
        completed = subprocess.run(cmd, text=True, capture_output=True, check=False)
        latency = round(time.monotonic() - started, 3)
        http_code = completed.stdout.strip()

        if completed.returncode != 0:
            print(f"[{index}/5] curl failed: {completed.stderr}", file=sys.stderr)
            return 2

        raw = response_file.read_text(encoding="utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            print(f"[{index}/5] non-JSON response (HTTP {http_code}): {raw[:500]}", file=sys.stderr)
            return 3

        safe_payload = redact_image_payload(payload)
        response_file.write_text(
            json.dumps(safe_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        if not str(http_code).startswith("2") or not payload.get("success", False):
            print(
                f"[{index}/5] API failure HTTP={http_code}: "
                f"{json.dumps(safe_payload, ensure_ascii=False)[:1200]}",
                file=sys.stderr,
            )
            return 4

        image_b64 = ((payload.get("result") or {}).get("image"))
        if not isinstance(image_b64, str) or not image_b64:
            print(f"[{index}/5] successful response missing result.image", file=sys.stderr)
            return 5

        if image_b64.startswith("data:"):
            image_b64 = image_b64.split(",", 1)[1]

        image_bytes = base64.b64decode(image_b64, validate=True)
        ext, mime = detect_image_type(image_bytes)
        image_path = ROOT / f"{slug}{ext}"
        image_path.write_bytes(image_bytes)

        headers_text = headers_file.read_text(encoding="utf-8", errors="replace")
        interesting_headers = [
            line.strip()
            for line in headers_text.splitlines()
            if any(term in line.lower() for term in ("cf-ray", "usage", "neuron", "cost", "billing", "unit"))
        ]

        usage_like = collect_usage_like(payload)
        api_neuron_signals = [
            item for item in usage_like if "neuron" in item["path"].lower()
        ]

        record = {
            "index": index,
            "slug": slug,
            "subject": subject,
            "prompt": prompt,
            "http_status": int(http_code),
            "latency_seconds": latency,
            "file": str(image_path),
            "mime": mime,
            "bytes": len(image_bytes),
            "sha256": hashlib.sha256(image_bytes).hexdigest(),
            "api_usage_like_fields": usage_like,
            "api_neuron_signals": api_neuron_signals,
            "interesting_response_headers": interesting_headers,
            "published_derived_neurons": round(derived_neurons, 2),
        }
        manifest["images"].append(record)

        actual = api_neuron_signals if api_neuron_signals else "not exposed in inference response"
        print(
            f"[{index}/5] OK {image_path.name} bytes={len(image_bytes)} "
            f"latency={latency}s API_NEURONS={actual} "
            f"PUBLISHED_DERIVED={derived_neurons:.2f}"
        )

    manifest["summary"] = {
        "count": len(manifest["images"]),
        "all_success": len(manifest["images"]) == 5,
        "api_reported_neurons_available_for_every_image": all(
            bool(item["api_neuron_signals"]) for item in manifest["images"]
        ),
        "published_derived_total_neurons": round(derived_neurons * len(manifest["images"]), 2),
    }

    (ROOT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (ROOT / "README.txt").write_text(
        "Isolated Cloudflare FLUX.2 Klein 4B probe.\n"
        "No production integration was performed.\n"
        "See manifest.json for prompts, hashes, latency, API telemetry, and published-pricing neuron derivation.\n",
        encoding="utf-8",
    )

    print("PROBE_COMPLETE=1")
    print(f"OUTPUT_DIR={ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
