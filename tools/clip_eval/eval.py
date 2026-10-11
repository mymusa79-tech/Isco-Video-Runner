"""Measure whether CLIP text-image similarity separates frames the cloud judge
accepted (PROOF matched) from rejected ones. Run 120 evidence, 14 candidates."""
import glob, json, os
import open_clip, torch
from PIL import Image

here = os.path.dirname(__file__)
items = json.load(open(os.path.join(here, "labels.json")))
model, _, pre = open_clip.create_model_and_transforms("ViT-B-32", pretrained="laion2b_s34b_b79k")
tok = open_clip.get_tokenizer("ViT-B-32"); model.eval()
rows = []
with torch.no_grad():
    for it in items:
        frames = sorted(glob.glob(os.path.join(here, "frames", it["dir"], "frame-*.jpg")))
        img = torch.stack([pre(Image.open(f).convert("RGB")) for f in frames])
        ie = model.encode_image(img); ie /= ie.norm(dim=-1, keepdim=True)
        te = model.encode_text(tok([it["query"]])); te /= te.norm(dim=-1, keepdim=True)
        sims = (ie @ te.T).squeeze(1)
        rows.append({**it, "clip_mean": float(sims.mean()), "clip_max": float(sims.max())})
pos = [r["clip_mean"] for r in rows if r["proof"] == "matched"]
neg = [r["clip_mean"] for r in rows if r["proof"] != "matched"]
auc = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / max(1, len(pos) * len(neg))
for r in sorted(rows, key=lambda r: -r["clip_mean"]):
    print(f"::notice::{r['clip_mean']:.3f} {r['proof']:11} {r['status']:5} {r['dir']} | {r['observed'][:70]}")
print(f"::notice::AUC matched-vs-not = {auc:.3f} (pos={len(pos)}, neg={len(neg)}); chance=0.5")
