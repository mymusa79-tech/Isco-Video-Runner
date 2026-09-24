#!/usr/bin/env python3
from __future__ import annotations

import base64, hashlib, json, os, re, subprocess, sys, time
from pathlib import Path

MODEL = "@cf/black-forest-labs/flux-2-klein-4b"
OUT = Path("artifacts/flux2-arabic-text-probe")
OUT.mkdir(parents=True, exist_ok=True)

SCENES = [
    {
        "slug": "01-arabic-short",
        "width": 1024, "height": 1536,
        "prompt": (
            "Create a premium cinematic vertical poster. Dark realistic room, warm side light, clean composition. "
            "Render this EXACT Arabic phrase as large, clean, centered headline text in the image: «خمس دقائق فقط». "
            "The Arabic letters must be correctly shaped and connected, right-to-left, with correct spelling, no extra words, no substitutions. "
            "Use a bold geometric Arabic display style, white text with one gold emphasis if natural. No logo, no watermark."
        ),
    },
    {
        "slug": "02-arabic-action",
        "width": 1024, "height": 1536,
        "prompt": (
            "Create a clean cinematic motivational vertical poster with sunrise light over a quiet city. "
            "Render this EXACT Arabic phrase prominently and legibly: «ابدأ اليوم». "
            "Correct Arabic shaping, connected letters, right-to-left order, exact spelling, no added text, no logo, no watermark."
        ),
    },
    {
        "slug": "03-arabic-question",
        "width": 1536, "height": 1024,
        "prompt": (
            "Create a cinematic YouTube thumbnail background with a tired person at a desk at night and a calm empty area for headline. "
            "Render this EXACT Arabic headline inside the image: «لماذا يضيع وقتك؟». "
            "Arabic must be correctly shaped and connected, right-to-left, exact spelling and punctuation, large and highly legible. "
            "No extra text, no logo, no watermark."
        ),
    },
    {
        "slug": "04-english-control",
        "width": 1536, "height": 1024,
        "prompt": (
            "Create a cinematic YouTube thumbnail background with a tired person at a desk at night. "
            "Render this EXACT English headline inside the image: "START TODAY". "
            "Large clean bold typography, exact spelling, no extra text, no logo, no watermark."
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
        response = OUT / f"{s['slug']}.response.json"
        headers = OUT / f"{s['slug']}.headers.txt"
        cmd = [
            "curl","--silent","--show-error","--location",
            "--dump-header",str(headers),
            "--output",str(response),
            "--write-out","%{http_code}",
            "--request","POST","--url",url,
            "--header",f"Authorization: Bearer {token}",
            "--form-string",f"prompt={s['prompt']}",
            "--form-string",f"width={s['width']}",
            "--form-string",f"height={s['height']}",
        ]
        t=time.monotonic()
        cp=subprocess.run(cmd,text=True,capture_output=True)
        dt=round(time.monotonic()-t,3)
        if cp.returncode:
            print(cp.stderr,file=sys.stderr); return 2
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
            "sha256":hashlib.sha256(data).hexdigest(),"prompt":s["prompt"],
        })
        print(f"[{i}/4] OK {image_path.name} neurons={n:.2f} latency={dt}s")
    manifest={"model":MODEL,"production_integration":False,"images":rows,
              "summary":{"count":len(rows),"all_success":len(rows)==4,"actual_cf_ai_neurons_total":round(total,2)}}
    (OUT/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(f"PROBE_COMPLETE=1 TOTAL_NEURONS={total:.2f}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
