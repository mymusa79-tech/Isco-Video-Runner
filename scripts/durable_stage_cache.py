from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def _remove_path(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def prepare_cache_for_persistence(root: Path) -> bool:
    """Sanitize each durable-stage namespace before the shared Actions cache is saved.

    This module owns transport persistence only. TTS and Media keep independent semantic
    fingerprints/validators; adding a new stage must never alter an older stage's cache
    identity merely because they share the same GitHub Actions cache directory.
    """
    root = Path(root)
    if root.is_symlink():
        _remove_path(root)
        return False
    root.mkdir(parents=True, exist_ok=True)

    tts_valid = False
    try:
        from scripts.tts_durable_cache import prepare_cache_for_persistence as prepare_tts

        tts_valid = bool(prepare_tts(root))
    except Exception as exc:
        _remove_path(root / "entries")
        print(f"TTS durable namespace sanitization rejected ({type(exc).__name__})")

    media_root = root / "media"
    media_shot_valid = False
    media_prepared_live_valid = False
    media_search_valid = False
    if media_root.exists():
        if media_root.is_symlink():
            _remove_path(media_root)
        else:
            try:
                from scripts.media_durable_cache import prepare_cache_for_persistence as prepare_media

                media_shot_valid = bool(prepare_media(media_root))
            except Exception as exc:
                for child in ("raw", "audits", "prepared"):
                    _remove_path(media_root / child)
                print(f"Media shot namespace sanitization rejected ({type(exc).__name__})")

            try:
                from scripts.media_prepared_live_cache import (
                    prepare_cache_for_persistence as prepare_media_prepared_live,
                )

                media_prepared_live_valid = bool(prepare_media_prepared_live(media_root))
            except Exception as exc:
                _remove_path(media_root / "prepared-live")
                print(f"Media prepared-live namespace sanitization rejected ({type(exc).__name__})")

            try:
                from scripts.media_search_durable_cache import (
                    prepare_cache_for_persistence as prepare_media_search,
                )

                media_search_valid = bool(prepare_media_search(media_root))
            except Exception as exc:
                _remove_path(media_root / "search")
                print(f"Media search namespace sanitization rejected ({type(exc).__name__})")

    allowed = tts_valid or media_shot_valid or media_prepared_live_valid or media_search_valid
    print(
        "Durable stage cache sanitized: "
        f"tts={tts_valid} media_shot={media_shot_valid} "
        f"media_prepared_live={media_prepared_live_valid} media_search={media_search_valid} "
        f"save_allowed={allowed}"
    )
    return allowed


def _main() -> int:
    parser = argparse.ArgumentParser(description="Sanitize shared Isco durable stage cache")
    parser.add_argument("prepare", nargs="?")
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    allowed = prepare_cache_for_persistence(Path(args.root))
    print(f"save_allowed={'true' if allowed else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
