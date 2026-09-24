#!/usr/bin/env python3
"""
Isolated Nidaa Al-Yaqza visual stress probe on Cloudflare Workers AI.
- Model: @cf/black-forest-labs/flux-2-klein-4b
- Exactly 8 images
- No production imports, no pipeline integration, no PR required.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

MODEL = "@cf/black-forest-labs/flux-2-klein-4b"
OUT = Path("artifacts/nidaa-flux2-probe")
OUT.mkdir(parents=True, exist_ok=True)

STYLE = (
    "Photorealistic cinematic still for an Arabic self-development channel. "
    "Emotionally clear visual storytelling, realistic everyday details, natural lens, "
    "soft cinematic contrast, coherent warm-neutral color grade with restrained gold highlights. "
    "Respectful Arab/global environment, no logos, no watermarks, no text, no typography, "
    "no captions, no readable writing. Avoid exaggerated fantasy unless the subject explicitly calls "
    "for symbolic imagery. Leave intentional clean negative space where specified for later Arabic title overlay."
)

SCENES = [
    {
        "slug": "01-short-night-phone-regret",
        "kind": "short_thumbnail",
        "width": 1024, "height": 1536,
        "subject": (
            "Late-night bedroom. A young adult seen mostly from the side/back sits on the floor beside the bed, "
            "lit mainly by a smartphone, tired and regretful after losing hours to endless scrolling. "
            "Desk with closed notebook and untouched plans in the background, clock near 3 AM, subtle mess. "
            "Dark blue-black room with a narrow warm doorway glow. Leave clean dark negative space in the upper-left."
        ),
    },
    {
        "slug": "02-short-time-slipping-away",
        "kind": "short_thumbnail",
        "width": 1024, "height": 1536,
        "subject": (
            "Symbolic but realistic scene about time being consumed by short-form video. "
            "Large glass hourglass on a dark table; inside the falling sand are tiny generic glowing phone-screen shapes "
            "without logos or readable interfaces, visually suggesting minutes disappearing into scrolling. "
            "Dramatic black background, subtle warm highlights, clean negative space on the left."
        ),
    },
    {
        "slug": "03-short-procrastination-clock-desk",
        "kind": "short_thumbnail",
        "width": 1024, "height": 1536,
        "subject": (
            "A cluttered work desk at night showing procrastination: analog clock, phone face-down, open blank notebook, "
            "crumpled paper, unfinished task materials, cold moonlight mixed with one warm desk lamp. "
            "No person needed. The image should immediately communicate that time has passed while work remained undone. "
            "Leave clean negative space in the upper-right."
        ),
    },
    {
        "slug": "04-explainer-overwhelmed-floor",
        "kind": "explainer_still",
        "width": 1024, "height": 1536,
        "subject": (
            "A young adult in a modest room sitting on the floor beside a desk, shoulders heavy, surrounded by unfinished "
            "notes and ordinary responsibilities. The emotion is overwhelmed and mentally stuck, not despair. "
            "Face not clearly visible. A small warm light source suggests there is still a way forward."
        ),
    },
    {
        "slug": "05-explainer-comparison-window",
        "kind": "explainer_still",
        "width": 1024, "height": 1536,
        "subject": (
            "A person seen from behind at a window in early morning, phone lowered in one hand after comparing their life "
            "to others online. Outside, ordinary people are beginning their day. Interior slightly dim, exterior warmer. "
            "The composition should communicate comparison, hesitation, and a quiet decision to return to real life."
        ),
    },
    {
        "slug": "06-explainer-said-tomorrow-again",
        "kind": "explainer_still",
        "width": 1024, "height": 1536,
        "subject": (
            "Morning light entering a room after another unproductive night. A chair by a desk, a phone charging, "
            "a blank open notebook, untouched exercise shoes, and a cold cup. No person. "
            "Visual meaning: many intentions, repeated postponement, but a new day has arrived."
        ),
    },
    {
        "slug": "07-long-dark-hook-thumbnail",
        "kind": "long_thumbnail",
        "width": 1536, "height": 1024,
        "subject": (
            "Cinematic YouTube thumbnail background about silent burnout and wasted time. "
            "A young adult seen from behind at a desk late at night, one hand on forehead, phone glowing on the desk, "
            "city lights outside. Strong emotional focal point on the left third, large clean dark negative space on the right "
            "for later Arabic headline. Realistic, premium, not melodramatic."
        ),
    },
    {
        "slug": "08-long-hope-turnaround-thumbnail",
        "kind": "long_thumbnail",
        "width": 1536, "height": 1024,
        "subject": (
            "Cinematic YouTube thumbnail background about beginning to win again after a difficult period. "
            "Same type of ordinary young adult seen from behind/three-quarter view opening curtains at sunrise, "
            "simple tidy workspace coming back to life, notebook open, shoes ready by the door. "
            "Warm hopeful morning, restrained victory rather than celebration. Subject on the right third, clean negative "
            "space on the left for later Arabic headline."
        ),
    },
]


def need(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def image_ext(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    raise ValueError(f"unknown image signature {data[:12]!r}")


def neuron_header(text: str) -> float | None:
    m = re.search(r"(?im)^cf-ai-neurons:\s*([0-9.]+)\s*$", text)
    return float(m.group(1)) if m else None


def main() -> int:
    token = need("CLOUDFLARE_API_TOKEN")
    account = need("CLOUDFLARE_ACCOUNT_ID")
    endpoint = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{MODEL}"

    manifest = {
        "experiment": "nidaa-flux2-thumbnail-explainer-stress-test",
        "model": MODEL,
        "production_integration": False,
        "style": STYLE,
        "images": [],
    }

    actual_total = 0.0

    for idx, scene in enumerate(SCENES, start=1):
        prompt = f"{STYLE} Subject: {scene['subject']}"
        raw_path = OUT / f"{scene['slug']}.response.json"
        headers_path = OUT / f"{scene['slug']}.headers.txt"

        cmd = [
            "curl", "--silent", "--show-error", "--location",
            "--dump-header", str(headers_path),
            "--output", str(raw_path),
            "--write-out", "%{http_code}",
            "--request", "POST",
            "--url", endpoint,
            "--header", f"Authorization: Bearer {token}",
            "--form-string", f"prompt={prompt}",
            "--form-string", f"width={scene['width']}",
            "--form-string", f"height={scene['height']}",
        ]

        t0 = time.monotonic()
        cp = subprocess.run(cmd, text=True, capture_output=True)
        elapsed = round(time.monotonic() - t0, 3)
        if cp.returncode != 0:
            print(cp.stderr, file=sys.stderr)
            return 2

        code = cp.stdout.strip()
        raw = raw_path.read_text(encoding="utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            print(f"[{idx}/8] non-JSON HTTP={code}: {raw[:500]}", file=sys.stderr)
            return 3

        safe = json.loads(json.dumps(payload))
        if isinstance((safe.get("result") or {}).get("image"), str):
            safe["result"]["image"] = "<base64 omitted>"
        raw_path.write_text(json.dumps(safe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        if not code.startswith("2") or not payload.get("success"):
            print(f"[{idx}/8] API failure HTTP={code}: {json.dumps(safe, ensure_ascii=False)[:1000]}", file=sys.stderr)
            return 4

        b64 = (payload.get("result") or {}).get("image")
        if not isinstance(b64, str) or not b64:
            print(f"[{idx}/8] missing result.image", file=sys.stderr)
            return 5
        if b64.startswith("data:"):
            b64 = b64.split(",", 1)[1]

        data = base64.b64decode(b64, validate=True)
        ext = image_ext(data)
        image_path = OUT / f"{scene['slug']}{ext}"
        image_path.write_bytes(data)

        header_text = headers_path.read_text(encoding="utf-8", errors="replace")
        neurons = neuron_header(header_text)
        if neurons is None:
            print(f"[{idx}/8] missing cf-ai-neurons header", file=sys.stderr)
            return 6
        actual_total += neurons

        rec = {
            "index": idx,
            "slug": scene["slug"],
            "kind": scene["kind"],
            "width": scene["width"],
            "height": scene["height"],
            "http_status": int(code),
            "latency_seconds": elapsed,
            "cf_ai_neurons": neurons,
            "file": str(image_path),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "prompt": prompt,
        }
        manifest["images"].append(rec)
        print(f"[{idx}/8] OK {image_path.name} neurons={neurons:.2f} latency={elapsed}s")

    manifest["summary"] = {
        "count": len(manifest["images"]),
        "all_success": len(manifest["images"]) == 8,
        "actual_cf_ai_neurons_total": round(actual_total, 2),
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"PROBE_COMPLETE=1 TOTAL_NEURONS={actual_total:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
