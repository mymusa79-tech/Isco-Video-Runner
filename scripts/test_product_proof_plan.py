from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import product_proof_plan  # noqa: E402  (needs sys.path fixup above)
import task_level_planner_router as router  # noqa: E402

import isco_video_agent.orchestrator as orchestrator  # noqa: E402


class ResilientRouterMarkerSurvivesProductProofInstallTests(unittest.TestCase):
    """Covers run 31870165348: after the __module__-vs-marker guard fix, the real CI
    run still raised "Resilient planner router is not installed" - one level deeper
    than the bug that fix addressed. install_router() sets the marker correctly, but
    run_v3_voice.py's main() calls install_product_proof_fallback() right after it,
    which reassigns orchestrator.build_plan to a brand-new `wrapped` function that
    calls through to the routed original but never copied its marker attribute -
    silently stripping it even though the router was still genuinely underneath.

    This test calls the real install_router() and the real
    install_product_proof_fallback(), in the same order run_v3_voice.py uses, then
    calls orchestrator's own real guard - exactly the failure this class of bug keeps
    reproducing when verified with a hand-rolled substitute instead of the real
    installers."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        gemini_key_path = Path(self._tmpdir.name) / "gemini_key"
        gemini_key_path.write_text("fake-gemini-key", encoding="utf-8")
        self._env_patch = patch.dict(os.environ, {"GEMINI_API_KEY_FILE": str(gemini_key_path)}, clear=False)
        self._env_patch.start()
        self._cache_patch = patch.object(router, "CACHE_PATH", Path(self._tmpdir.name) / "planning-checkpoint.json")
        self._cache_patch.start()

    def tearDown(self) -> None:
        self._cache_patch.stop()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def test_real_installer_chain_satisfies_orchestrators_own_guard(self) -> None:
        router.install_router()
        product_proof_plan.install_product_proof_fallback()
        self.assertTrue(getattr(orchestrator.build_plan, "_is_resilient_router", False))
        orchestrator._verify_resilient_router_installed()  # must not raise


if __name__ == "__main__":
    unittest.main()
