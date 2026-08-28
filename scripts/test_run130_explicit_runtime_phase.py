from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import runtime_closure
from scripts import runtime_phase


class Run130ExplicitRuntimePhaseTests(unittest.TestCase):
    def _workflow_env(self) -> dict[str, str]:
        return {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_WORKFLOW_REF": (
                "mymusa79-tech/Isco-Video-Runner/.github/workflows/"
                "produce-resilient-v4.yml@refs/heads/main"
            ),
        }

    def test_workflow_identity_alone_does_not_activate_runtime(self) -> None:
        with patch.dict(os.environ, self._workflow_env(), clear=True):
            self.assertTrue(runtime_phase.canonical_workflow_identity())
            self.assertFalse(runtime_phase.canonical_runtime_enabled())

    def test_explicit_runtime_still_requires_canonical_workflow_identity(self) -> None:
        env = self._workflow_env()
        env["ISCO_CANONICAL_RUNTIME"] = "1"
        env["GITHUB_WORKFLOW_REF"] = (
            "mymusa79-tech/Isco-Video-Runner/.github/workflows/verify-private-engine.yml@refs/heads/main"
        )
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(runtime_phase.canonical_workflow_identity())
            self.assertFalse(runtime_phase.canonical_runtime_enabled())

    def test_activation_updates_current_process_and_later_steps(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            github_env = Path(td) / "github-env"
            env = self._workflow_env()
            env["GITHUB_ENV"] = str(github_env)
            with patch.dict(os.environ, env, clear=True):
                runtime_phase.activate_canonical_runtime()
                self.assertTrue(runtime_phase.canonical_runtime_enabled())
                self.assertEqual(os.environ["ISCO_CANONICAL_RUNTIME"], "1")
                self.assertIn("ISCO_CANONICAL_RUNTIME=1", github_env.read_text(encoding="utf-8"))

    def test_run130_preproduction_context_does_not_bind_runtime_snapshot_or_persistence(self) -> None:
        installer_names = (
            "install_attempt10_append_bound_recovery",
            "install_bounded_output_recovery",
            "install_schema_repair_policy",
            "install_gemini_planning_output_guard",
            "install_run124_terminal_provider_recovery",
            "install_run125_capacity_routing_closure",
            "install_dynamic_planning_capacity",
            "install_run125_cache_prefix_contract",
            "certify_runtime_patch_contracts",
            "install_provider_capacity_v2",
            "install_media_trust_boundary_v2",
            "install_core_reliability_guard",
            "install_audio_semantic_integrity_binding",
            "install_audio_mastering_live_binding",
            "install_sfx_live_binding",
            "install_m8_live_binding",
            "install_m9_live_binding",
            "install_m10_live_binding",
            "install_cta_live_binding",
            "install_narrative_music_dynamics",
            "install_canonical_v4_bundle_post_manifest",
            "install_release_transaction_guard",
            "install_telemetry_reliability_binding",
            "install_audio_semantic_final_gate",
        )
        env = self._workflow_env()
        with patch.dict(os.environ, env, clear=True), patch.object(
            runtime_closure, "install_runtime_snapshot_binding"
        ) as snapshot, patch.object(
            runtime_closure, "install_runtime_persistence_wrapper"
        ) as persistence:
            patches = [patch.object(runtime_closure, name) for name in installer_names]
            mocks = [item.start() for item in patches]
            try:
                runtime_closure.install_runtime_closure()
            finally:
                for item in reversed(patches):
                    item.stop()
            self.assertEqual(len(mocks), len(installer_names))
            snapshot.assert_not_called()
            persistence.assert_not_called()

    def test_live_runtime_context_binds_snapshot_and_persistence(self) -> None:
        env = self._workflow_env()
        env["ISCO_CANONICAL_RUNTIME"] = "1"
        installer_names = (
            "install_attempt10_append_bound_recovery",
            "install_bounded_output_recovery",
            "install_schema_repair_policy",
            "install_gemini_planning_output_guard",
            "install_run124_terminal_provider_recovery",
            "install_run125_capacity_routing_closure",
            "install_dynamic_planning_capacity",
            "install_run125_cache_prefix_contract",
            "certify_runtime_patch_contracts",
            "install_provider_capacity_v2",
            "install_media_trust_boundary_v2",
            "install_core_reliability_guard",
            "install_audio_semantic_integrity_binding",
            "install_audio_mastering_live_binding",
            "install_sfx_live_binding",
            "install_m8_live_binding",
            "install_m9_live_binding",
            "install_m10_live_binding",
            "install_cta_live_binding",
            "install_narrative_music_dynamics",
            "install_canonical_v4_bundle_post_manifest",
            "install_release_transaction_guard",
            "install_telemetry_reliability_binding",
            "install_audio_semantic_final_gate",
        )
        with patch.dict(os.environ, env, clear=True), patch.object(
            runtime_closure, "install_runtime_snapshot_binding"
        ) as snapshot, patch.object(
            runtime_closure, "install_runtime_persistence_wrapper"
        ) as persistence:
            patches = [patch.object(runtime_closure, name) for name in installer_names]
            for item in patches:
                item.start()
            try:
                runtime_closure.install_runtime_closure()
            finally:
                for item in reversed(patches):
                    item.stop()
            snapshot.assert_called_once_with()
            persistence.assert_called_once_with(runtime_closure.orchestrator)

    def test_persistent_memory_owns_phase_transition_before_snapshot_bootstrap(self) -> None:
        source = (Path(__file__).with_name("persistent_memory.py")).read_text(encoding="utf-8")
        self.assertIn("canonical_workflow_identity()", source)
        self.assertIn("activate_canonical_runtime()", source)
        self.assertLess(
            source.index("activate_canonical_runtime()"),
            source.index("bootstrap_immutable_planning_checkpoint("),
        )


if __name__ == "__main__":
    unittest.main()
