#!/usr/bin/env python3
from __future__ import annotations

import base64, hashlib, json, os, re, subprocess, sys, time
from pathlib import Path

MODEL = "@cf/black-forest-labs/flux-2-klein-4b"
OUT = Path("artifacts/nidaa-flux2-bg-repair")
OUT.mkdir(parents=True, exist_ok=True)

STYLE = (
    "Photorealistic cinematic still for an Arabic self-development channel. "
    "Natural realistic room geometry, coherent continuous background from edge to edge, "
    "warm-neutral cinematic grade with restrained gold highlights, soft contrast, natural lens. "
    "No text, no typography, no logos, no watermarks. "
    "IMPORTANT: never create a solid black panel, blank rectangle, empty void, graphic card, or artificial text box. "
    "If negative space is requested, keep it as a naturally quieter real part of the room with visible texture and lighting."
)

SCENES = [
    {
        "slug": "01-long-continuous-bg",
        "width": 1536, "height": 1024,
        "prompt": (
            "Cinematic YouTube thumbnail background about silent burnout and wasted time. "
            "Exactly one young adult seen from behind at a desk late at night, one hand on forehead, phone on the desk, "
            "city lights outside. Subject occupies the left third. The right side must remain a real continuous part of the same "
            "room: softly lit wall, curtains, shelf or window reflections with subtle texture and enough calm contrast for later title overlay. "
            "Do not create any solid dark block, black panel, blank void, split-screen effect, poster board, or artificial empty rectangle."
        ),
    },
    {
        "slug": "02-short-one-person-bg",
        "width": 1024, "height": 1536,
        "prompt": (
            "Vertical short poster background about losing time to late-night scrolling. "
            "Exactly ONE young adult only, no second person, no reflections resembling another person, no duplicate body. "
            "The person sits alone beside the bed looking down at a phone, tired and regretful but not despairing. "
            "Dark blue-black room, one warm doorway glow, ordinary desk and clock in background. "
            "Keep the upper area naturally calm for later Arabic title overlay while preserving real wall/room texture. "
            "No solid black panel, no blank void, no graphic text box."
        ),
    },
]

def need(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit(f"missing {name}")
    return v

def ext(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"): return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"): return ".png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP": return ".webp"
    raise ValueError("unknown image type")

def neurons(headers: str) -> float | None:
    m = re.search(r"(?im)^cf-ai-neurons:\s*([0-9.]+)\s*$", headers)
    return float(m.group(1)) if m else None

def main() -> int:
    token = need("CLOUDFLARE_API_TOKEN")
    account = need("CLOUDFLARE_ACCOUNT_ID")
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{MODEL}"
    rows=[]; total=0.0
    for i, s in enumerate(SCENES, 1):
        prompt = STYLE + " Subject: " + s["prompt"]
        response = OUT / f"{s['slug']}.response.json"
        headers = OUT / f"{s['slug']}.headers.txt"
        cmd = [
            "curl","--silent","--show-error","--location",
            "--dump-header",str(headers),
            "--output",str(response),
            "--write-out","%{http_code}",
            "--request","POST","--url",url,
            "--header",f"Authorization: Bearer {token}",
            "--form-string",f"prompt={prompt}",
            "--form-string",f"width={s['width']}",
            "--form-string",f"height={s['height']}",
        ]
        t=time.monotonic()
        cp=subprocess.run(cmd,text=True,capture_output=True)
        dt=round(time.monotonic()-t,3)
        if cp.returncode: print(cp.stderr,file=sys.stderr); return 2
        code=cp.stdout.strip()
        payload=json.loads(response.read_text(encoding="utf-8"))
        safe=json.loads(json.dumps(payload))
        if isinstance((safe.get("result") or {}).get("image"),str):
            safe["result"]["image"]="<base64 omitted>"
        response.write_text(json.dumps(safe,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        if not code.startswith("2") or not payload.get("success"):
            print(json.dumps(safe,ensure_ascii=False),file=sys.stderr); return 3
        b64=(payload.get("result") or {}).get("image")
        if b64.startswith("data:"): b64=b64.split(",",1)[1]
        data=base64.b64decode(b64,validate=True)
        image_path=OUT/f"{s['slug']}{ext(data)}"
        image_path.write_bytes(data)
        n=neurons(headers.read_text(encoding="utf-8",errors="replace"))
        if n is None: return 4
        total += n
        rows.append({
            "index":i,"slug":s["slug"],"file":str(image_path),
            "width":s["width"],"height":s["height"],"http_status":int(code),
            "latency_seconds":dt,"cf_ai_neurons":n,"bytes":len(data),
            "sha256":hashlib.sha256(data).hexdigest(),"prompt":prompt,
        })
        print(f"[{i}/2] OK {image_path.name} neurons={n:.2f} latency={dt}s")
    manifest={"model":MODEL,"production_integration":False,"images":rows,
              "summary":{"count":len(rows),"all_success":len(rows)==2,"actual_cf_ai_neurons_total":round(total,2)}}
    (OUT/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(f"PROBE_COMPLETE=1 TOTAL_NEURONS={total:.2f}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
