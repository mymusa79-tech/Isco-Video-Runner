"""Measure whether CLIP text-image similarity separates frames the cloud judge
accepted (PROOF matched) from rejected ones. Run 120 evidence, 14 candidates."""
import glob, json, os

def main():
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
    print(f"::notice::AUC matched-vs-not = {auc:.3f} (pos={len(pos)}, neg={len(neg)}); chance=0.5; pos_mean={sum(pos)/len(pos):.3f} neg_mean={sum(neg)/len(neg):.3f}")
    ranked = sorted(rows, key=lambda r: -r["clip_mean"])
    print("::notice::RANK " + " ".join(f"{r['clip_mean']:.2f}:{'+' if r['proof']=='matched' else '-'}:{r['dir'][:2]}{r['dir'][3:5]}{r['dir'][-2:] if 'recovery' in r['dir'] else ''}" for r in ranked))
    for sec in ("s1","s2","s3"):
        grp = [r for r in rows if f"-{sec}-" in r["dir"]]
        top = max(grp, key=lambda r: r["clip_mean"])
        print(f"::notice::{sec} CLIP top pick = {top['dir']} proof={top['proof']} status={top['status']}")


if __name__ == "__main__":
    import traceback
    try:
        main()
    except Exception:
        for line in traceback.format_exc().splitlines()[-6:]:
            print("::error::" + line[:300])
        raise
