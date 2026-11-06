"""Pack approved Cinematic Monoclip JSON + still images into an importer-safe ZIP.

No TTS, API, stock download, music or publish request occurs here. The pinned
stock URL is syntactically checked; production must still admit the real clip.

Usage:
 python -m scripts.prepare_cinematic_monoclip_zip --manifest ./production.json --assets-root . --output ./approved-monoclip.zip
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

from clean_v2.owner_pack import OwnerPackError
from clean_v2.production_package import MAX_ZIP_BYTES, inspect_archive, validate_manifest

STYLES = frozenset({"bw_cinematic", "color_cinematic"})


def prepare_archive(manifest_path: Path, assets_root: Path, output: Path) -> dict[str, Any]:
    """Build an unrendered approved ZIP; never start paid TTS."""
    manifest_path = Path(manifest_path)
    assets_root = Path(assets_root).resolve(strict=True)
    output = Path(output)
    if not assets_root.is_dir():
        raise ValueError("assets-root must be a directory")
    if not manifest_path.is_file():
        raise FileNotFoundError(str(manifest_path))

    # Never fabricate Claude approval; it must already be present in the JSON.
    doc = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    validate_manifest(doc)
    if doc.get("format") != "short" or doc.get("visual_style") not in STYLES:
        raise ValueError("Only an explicitly opted-in Cinematic Monoclip can use this packer")

    names = {scene["asset"] for scene in doc["scenes"]}
    cover = doc["cover"].get("asset")
    if cover:
        names.add(cover)

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=5) as archive:
        archive.writestr("production.json", json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"))
        for name in sorted(names):
            # validate_manifest already checks the constrained asset filename.
            # Resolve again to ensure that symlinks cannot escape assets-root.
            image = (assets_root / name).resolve(strict=True)
            if not image.is_relative_to(assets_root) or not image.is_file():
                raise ValueError(f"Unsafe asset path: {name}")
            archive.write(image, arcname=name)
    data = stream.getvalue()
    if len(data) > MAX_ZIP_BYTES:
        raise ValueError("Monoclip ZIP exceeds the 18 MiB importer limit")
    inspected = inspect_archive(data)
    if inspected["manifest"]["visual_style"] != doc["visual_style"]:
        raise RuntimeError("Monoclip visual-style mismatch after ZIP validation")

    output.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite an existing approved ZIP: the importer pins its SHA256.
    with output.open("xb") as dest:
        dest.write(data)
    return {
        "output": str(output),
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "style": doc["visual_style"],
        "still_images": len(names),
        "stock_page_urls": (
            doc["scenes"][1]["stock"].get("urls")
            or [doc["scenes"][1]["stock"]["url"]]
        ),
        "voice_requests_made": 0,
        "stock_downloads_made": 0,
        "ready_to_publish": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--assets-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        summary = prepare_archive(args.manifest, args.assets_root, args.output)
    except (OSError, ValueError, TypeError, RuntimeError, OwnerPackError, json.JSONDecodeError) as error:
        print(f"Monoclip package FAILED: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
