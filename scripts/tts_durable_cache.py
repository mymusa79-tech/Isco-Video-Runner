from __future__ import annotations

import sys

from scripts import tts_durable_cache_semantics as _semantics

# Compatibility facade: runtime/test imports retain the historical module path while
# the immutable TTS semantic implementation lives byte-for-byte in the sibling module.
# Re-export private helpers as well because focused regression tests intentionally probe
# the cache contract internals.
for _name in dir(_semantics):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_semantics, _name)


def _main() -> int:
    # CLI invocation owns the shared durable-stage transport, not TTS semantics. This
    # lets Media/Render add isolated namespaces without altering the TTS fingerprint.
    from scripts.durable_stage_cache import _main as stage_cache_main

    return stage_cache_main()


if __name__ == "__main__":
    raise SystemExit(_main())
