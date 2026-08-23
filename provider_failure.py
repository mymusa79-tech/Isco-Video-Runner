"""Compatibility import for Runner modules loaded as the ``scripts`` package.

Production executes ``scripts/run_v3_voice.py`` in script mode, where the scripts
folder itself is on ``sys.path``. Unit and integration contracts import the same
runtime as ``scripts.run_v3_voice`` from the repository root. Keep both entry modes
bound to one provider-failure implementation instead of duplicating policy.
"""

from scripts.provider_failure import ProviderFailure, classify_provider_failure

__all__ = ["ProviderFailure", "classify_provider_failure"]
