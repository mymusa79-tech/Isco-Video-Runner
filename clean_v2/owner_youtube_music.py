"""Metadata-only owner-curated YouTube Audio Library track registry.

No music binaries are ever committed to GitHub. Selected owner-supplied MP3
travels INSIDE the manually approved private ZIP, hash-verified by importer.
YouTube attribution/license must still be confirmed in Studio per track.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Mapping

ASSET = "assets/music_bed.mp3"
SOURCE = "youtube_audio_library"
TRACKS: dict[str, dict[str, Any]] = {
    "forgiven-fate": {"title": "Forgiven Fate", "artist": "Dan Lebowitz",
        "sha256": "7f0818dfbabacba130750124df4afd67ba5bd8f9aeda1f860de254d492612152",
        "formats": ("ala_mahl", "short"), "mood": "warm_story"},
    "after-sunset": {"title": "After Sunset", "artist": "Alex Jones & Xander Jones",
        "sha256": "47c783337b3d28f52fb529605e651ecd4acc1bfc6edbe03f23335a08c0028335",
        "formats": ("podcast", "film"), "mood": "quiet_dialogue"},
    "nebula": {"title": "Nebula", "artist": "The Grey Room & Density & Time",
        "sha256": "b564b13379fb06f3d85afa8d40adf9a3a731446c0a9ea543778ea0e11d774250",
        "formats": ("podcast", "film"), "mood": "curiosity_ambient"},
    "lift-up": {"title": "Lift Up", "artist": "Mini Vandals",
        "sha256": "469d617f36011a1235f94127df9350f0bd239b6b89785798703af81f17c49832",
        "formats": ("short", "film"), "mood": "uplifting"},
    "demoiselle": {"title": "Demoiselle", "artist": "Rondo Brothers",
        "sha256": "d909876db3e7cdf850cc721c4d7cd9077ebe679dab948c09f2e89495f04a41ab",
        "formats": ("ala_mahl", "short"), "mood": "warm_playful"},
    "in-battery": {"title": "In Battery", "artist": "Density & Time",
        "sha256": "ca9d708ac6856eea67943637c927d8cd935bcae31180433471fca6bfa63c33f9",
        "formats": ("film",), "mood": "gentle_tension"},
}


def valid_track(track_id: object, fmt: str, template: str) -> bool:
    row = TRACKS.get(track_id) if isinstance(track_id, str) else None
    if not row:
        return False
    required = "ala_mahl" if template == "ala_mahl" else fmt
    return required in row["formats"]


def verify_audio(content: bytes, track_id: str) -> bool:
    """No blind MP3s, spoofed filenames, alternate media, or unapproved content."""
    row = TRACKS.get(track_id)
    return bool(
        row and 1024 < len(content) <= 12 * 1024 * 1024
        and (content.startswith(b"ID3") or (
            len(content) >= 2 and content[0] == 255 and content[1] & 224 == 224))
        and hashlib.sha256(content).hexdigest() == row["sha256"]
    )


def resolve_music_asset(output_dir: Path, editing: Mapping[str, Any]) -> Path:
    """The same private ZIP location used for approved photographic assets."""
    if editing.get("asset") != ASSET:
        raise RuntimeError("youtube_audio_library_asset_invalid")
    track_id = str(editing.get("track_id") or "")
    if track_id not in TRACKS:
        raise RuntimeError("youtube_audio_library_track_unknown")
    root = os.environ.get("CLEAN_V2_OWNER_ART_DIR") or ""
    options = [Path(root) / ASSET] if root else []
    # Local offline fixtures may use the render directory itself as ZIP root.
    options.append(Path(output_dir) / ASSET)
    for candidate in options:
        if candidate.is_file() and verify_audio(candidate.read_bytes(), track_id):
            return candidate
    raise RuntimeError("youtube_audio_library_track_missing_or_hash_mismatch")
