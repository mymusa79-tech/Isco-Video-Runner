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


class FallbackScopeRestrictionTests(unittest.TestCase):
    """Covers item 5: install_product_proof_fallback() must stay a narrow last-resort
    safety net for exactly one hardcoded, pre-approved topic + film format - never a
    general "write something plausible for whatever topic failed" fallback. Verified
    directly against the real wrapper (patching only orchestrator.build_plan, the one
    thing install_product_proof_fallback() itself replaces) rather than re-deriving
    the restriction logic by hand, so this actually exercises the shipped code."""

    def setUp(self) -> None:
        self._build_plan_patch = patch.object(orchestrator, "build_plan")
        self._mock_build_plan = self._build_plan_patch.start()

    def tearDown(self) -> None:
        self._build_plan_patch.stop()

    def test_matching_topic_and_format_triggers_fallback_only_after_a_real_failure(self) -> None:
        self._mock_build_plan.side_effect = RuntimeError("cloud planning failed")
        product_proof_plan.install_product_proof_fallback()

        plan = orchestrator.build_plan("key", product_proof_plan._PROOF_TOPIC, "film", "gemini-2.5-flash")

        self.assertEqual(plan.topic, product_proof_plan._PROOF_TOPIC)
        self.assertTrue(product_proof_plan.was_fallback_used())

    def test_different_topic_with_the_same_underlying_failure_is_not_papered_over(self) -> None:
        self._mock_build_plan.side_effect = RuntimeError("cloud planning failed")
        product_proof_plan.install_product_proof_fallback()

        with self.assertRaisesRegex(RuntimeError, "cloud planning failed"):
            orchestrator.build_plan("key", "موضوع إنتاج حقيقي مختلف تمامًا", "film", "gemini-2.5-flash")
        self.assertFalse(product_proof_plan.was_fallback_used())

    def test_matching_topic_wrong_format_is_not_papered_over(self) -> None:
        self._mock_build_plan.side_effect = RuntimeError("cloud planning failed")
        product_proof_plan.install_product_proof_fallback()

        with self.assertRaisesRegex(RuntimeError, "cloud planning failed"):
            orchestrator.build_plan("key", product_proof_plan._PROOF_TOPIC, "story", "gemini-2.5-flash")
        self.assertFalse(product_proof_plan.was_fallback_used())

    def test_matching_topic_and_format_with_no_failure_never_engages_the_fallback(self) -> None:
        real_plan = object()
        self._mock_build_plan.return_value = real_plan
        product_proof_plan.install_product_proof_fallback()

        result = orchestrator.build_plan("key", product_proof_plan._PROOF_TOPIC, "film", "gemini-2.5-flash")

        self.assertIs(result, real_plan)
        self.assertFalse(product_proof_plan.was_fallback_used())


if __name__ == "__main__":
    unittest.main()
