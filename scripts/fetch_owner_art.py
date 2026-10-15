#!/usr/bin/env python3
"""Download the owner's Podcast images from Telegram into a directory (zero cost, no Engine).

Input: the request's ``owner_art`` JSON ({"body": [{file_id,...}], "cover": {..}|null}).
Output: <out>/image-1.jpg .. image-N.jpg and <out>/cover.jpg, all normalised to 1920x1080
(cover to 1280x720 later by the pipeline). Fails loudly: a run that promised the owner
his own images must not silently fall back to stock.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from clean_v2 import owner_art  # noqa: E402

MAX_BYTES = 25 * 1024 * 1024


def _api(token: str, method: str, **params: str) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not value.get("ok"):
        raise RuntimeError(f"telegram {method} failed")
    return value["result"]


def download(token: str, file_id: str, destination: Path) -> Path:
    info = _api(token, "getFile", file_id=file_id)
    if int(info.get("file_size") or 0) > MAX_BYTES:
        raise RuntimeError("owner image is larger than 25MB")
    url = f"https://api.telegram.org/file/bot{token}/{info['file_path']}"
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise RuntimeError("owner image is larger than 25MB")
    destination.write_bytes(data)
    return destination


def fetch(art: dict, out: Path, token: str, fetcher=download) -> list[str]:
    out.mkdir(parents=True, exist_ok=True)
    raw = out / "raw"
    raw.mkdir(exist_ok=True)
    written: list[str] = []
    for index, item in enumerate(art.get("body") or [], start=1):
        source = fetcher(token, str(item["file_id"]), raw / f"body-{index}")
        owner_art.normalize_image(source, out / f"image-{index}.jpg")
        written.append(f"image-{index}.jpg")
    cover = art.get("cover")
    if cover:
        source = fetcher(token, str(cover["file_id"]), raw / "cover")
        owner_art.normalize_image(source, out / "cover.jpg", owner_art.COVER_SIZE)
        written.append("cover.jpg")
    for leftover in raw.iterdir():
        leftover.unlink()
    raw.rmdir()
    return written


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--art-json", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required to fetch the owner's images")
    art = json.loads(args.art_json.read_text(encoding="utf-8"))
    written = fetch(art, args.out, token)
    print(json.dumps({"owner_art_files": written}, ensure_ascii=False))


if __name__ == "__main__":
    main()
