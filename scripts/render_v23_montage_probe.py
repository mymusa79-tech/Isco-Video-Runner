"""Offline montage comparison; run from either candidate or baseline repo root.

Uses the existing production pipeline, deterministic test pictures/tones, and real
FFmpeg identity/text/CTA passes. Engine post-render kernels and mastering/QC are
test doubles: this is not a production run or an audition of provider voices.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path.cwd()))

from PIL import Image, ImageDraw
from clean_v2 import owner_art, owner_pack, production_package
from clean_v2.contracts import atomic_write_json, compute_brief_sha256
from clean_v2.media import inspect_final
from clean_v2.pipeline import CleanV2Pipeline, _run_legacy_cinematic_layer
from scripts.test_clean_v2 import _passing_audio_mastering, _passing_final_master_qc, _passing_visual_qa
from scripts.test_production_package import ala_mahl_manifest, manifest
from scripts.test_ready_production_e2e import LocalVoice


def fixture(kind):
    doc = ala_mahl_manifest(2) if kind == "ala_mahl" else manifest(kind)
    for scene in doc["scenes"]:
        scene["motion"] = "hold"
        scene["transition"] = "cut"
    # Legal on old and new clients: the author keeps one image/move across two
    # sections. Before V23 the section boundary restarts the move.
    if kind in {"short", "film"}:
        doc["scenes"][2]["asset"] = doc["scenes"][1]["asset"]
        doc["scenes"][1]["motion"] = doc["scenes"][2]["motion"] = "slow_pan"
    if kind == "podcast":
        # Several A/B turns under one picture; short sentences also exercise
        # sparse key text. Never generate or rewrite production narration.
        for section in doc["script"]["sections"]:
            section["turns"].extend([
                {"speaker": "A", "text": "لكن كيف أبدأ دون خطة جديدة؟"},
                {"speaker": "B", "text": "أكمل خطوة صغيرة، ثم راجع ما حدث."},
            ])
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("production.json", json.dumps(doc, ensure_ascii=False))
        for index, name in enumerate(sorted({s["asset"] for s in doc["scenes"]})):
            size = (360, 640) if doc["format"] == "short" else (640, 360)
            picture = Image.new("RGB", size, (92 + index * 12, 86 + index * 9, 78 + index * 7))
            draw = ImageDraw.Draw(picture)
            for x in range(10, size[0], 35):
                draw.line((x, 0, x, size[1]), fill=(130, 123, 110), width=2)
            draw.rectangle((size[0]//5, size[1]//5, size[0]//2, size[1]//2), fill=(48, 57, 60))
            draw.ellipse((size[0]//2, 40, size[0]*4//5, size[1]//3), fill=(166, 146, 119))
            data = io.BytesIO(); picture.save(data, "PNG")
            archive.writestr(name, data.getvalue())
    return raw.getvalue()


def render(kind, root):
    root.mkdir(parents=True, exist_ok=False)
    data = fixture(kind)
    checked = production_package.inspect_archive(data)
    doc = checked["manifest"]
    art = root / "art"; output = root / "out"
    production_package.extract_archive(data, art, expected_sha256=checked["zip_sha256"])
    (root / "input.zip").write_bytes(data)
    pack = {"schema_version": 1, "format": doc["format"], "title": doc["title"], "production": doc,
            "asset_sha256": checked["asset_sha256"], "zip_sha256": checked["zip_sha256"]}
    brief = {"approved_by_user": True, "approved_topic": doc["title"], "format": doc["format"], "language": "ar",
             "audience": "Arabic-speaking adults", "editorial_intent": "نص اختبار محلي ثابت", "research_pack": [],
             "hard_constraints": ["No fabricated facts."]}
    atomic_write_json(root / "pack.json", pack); atomic_write_json(root / "brief.json", brief)
    stock = mock.Mock(events=[]); router = mock.Mock(events=[])
    router.route.side_effect = AssertionError("no writing/provider calls")
    voice = LocalVoice()
    with mock.patch.dict(os.environ, {owner_pack.OWNER_PACK_ENV: str(root / "pack.json"),
                                     owner_art.ART_DIR_ENV: str(art), "CLEAN_V2_SIMPLE_MODE": "true", "CLEAN_V2_SCENE_REVIEW": ""}), \
         mock.patch("urllib.request.urlopen", side_effect=AssertionError("offline probe")), \
         mock.patch("clean_v2.legacy_cinematic.apply_post_render_layer", return_value={"status": "test_double_engine_unavailable"}):
        result = CleanV2Pipeline(
            router=router, voice_synthesizer=voice, visual_source=owner_art.OwnerArtVisualSource(stock, art),
            visual_qa=_passing_visual_qa, opening_director=lambda **kw: {"status": "skipped_simple_mode", "slots": []},
            cinematic_layer=_run_legacy_cinematic_layer, final_master_qc=_passing_final_master_qc,
            audio_mastering=_passing_audio_mastering,
        ).run(brief_path=root / "brief.json", approved_sha256=compute_brief_sha256(brief), output_dir=output,
              engine_sha="a"*40, runner_sha="b"*40)
    stock.acquire.assert_not_called(); router.route.assert_not_called()
    assets_after = {n: hashlib.sha256((art / n).read_bytes()).hexdigest() for n in checked["asset_sha256"]}
    assert assets_after == checked["asset_sha256"]
    summary = {"kind": kind, "status": result["status"], "media": inspect_final(output / "final.mp4"),
               "asset_sha256": assets_after, "spoken": [t for t, _ in voice.spoken],
               "mastered_audio_sha256": hashlib.sha256((output / "narration-mastered.wav").read_bytes()).hexdigest(),
               "synthetic_audio": True, "engine_post_render_test_double": True}
    atomic_write_json(root / "probe-summary.json", summary)
    print(json.dumps({"completed": kind, "duration": summary["media"]["duration_seconds"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--kinds", nargs="+", default=["podcast", "short", "ala_mahl", "film"])
    args = parser.parse_args()
    for kind in args.kinds:
        render(kind, args.output.resolve() / kind)
