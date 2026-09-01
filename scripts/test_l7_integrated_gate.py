from __future__ import annotations

import hashlib
import inspect
import unittest
from pathlib import Path
from unittest.mock import patch

import isco_video_agent.orchestrator as orchestrator

from scripts import orchestration_cinematic_port as cinematic_port
from scripts import orchestration_media_port as media_port
from scripts import orchestration_qc_port as qc_port
from scripts import orchestration_render_port as render_port
from scripts import orchestration_shorts_port as shorts_port
from scripts import orchestration_tts_port as tts_port
from scripts import run_control_production
from scripts import run_v3_voice
from scripts import runtime_closure
from scripts.orchestration_planning_registration_adapter import (
    PLANNING_CONTRACT_SOURCE_PATH,
    PLANNING_CONTRACT_SOURCE_SHA,
    PLANNING_RESOLVER_ID,
    PlanningRegistrationRequest,
    PlanningRequestKind,
    register_planning_adapter,
)
from scripts.orchestration_stage_registry import build_l4_registry


_PORTS = {
    "tts": (tts_port, Path("scripts/orchestration_tts_port.py")),
    "media": (media_port, Path("scripts/orchestration_media_port.py")),
    "cinematic": (cinematic_port, Path("scripts/orchestration_cinematic_port.py")),
    "render": (render_port, Path("scripts/orchestration_render_port.py")),
    "qc": (qc_port, Path("scripts/orchestration_qc_port.py")),
    "shorts": (shorts_port, Path("scripts/orchestration_shorts_port.py")),
}

_EXPECTED_CACHE = {
    "tts": (True, True),
    "media": (True, True),
    "cinematic": (False, False),
    "render": (True, True),
    "qc": (False, False),
    "shorts": (False, False),
}

_EXPECTED_SIDE_EFFECT = {
    "tts": "idempotent",
    "media": "idempotent",
    "cinematic": "none",
    "render": "idempotent",
    "qc": "none",
    "shorts": "idempotent",
}


def _git_blob_sha(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


class L7IntegratedGateTests(unittest.TestCase):
    def _assert_order(self, source: str, calls: tuple[str, ...]) -> None:
        cursor = -1
        for call in calls:
            position = source.find(call, cursor + 1)
            self.assertGreater(position, cursor, f"missing/out-of-order call: {call}")
            cursor = position

    def test_all_six_stage_contracts_bind_exact_stable_ports_and_preserve_owners(self) -> None:
        registry = build_l4_registry()
        self.assertEqual(registry.stage_ids(), ("cinematic", "media", "qc", "render", "shorts", "tts"))

        for stage_id, (port, path) in _PORTS.items():
            with self.subTest(stage=stage_id):
                contract = registry.get(stage_id)
                self.assertEqual(contract.implementation_binding.adapter_id, port.PORT_ID)
                self.assertEqual(contract.implementation_binding.source_path, str(path))
                self.assertEqual(contract.implementation_binding.source_sha, _git_blob_sha(path))
                self.assertEqual(contract.provider_policy["owner"], port.PROVIDER_OWNER)
                self.assertEqual(contract.retry_policy.owner, port.RETRY_OWNER)
                self.assertTrue(contract.retry_policy.bounded)
                self.assertEqual(
                    (contract.cache_policy.read, contract.cache_policy.write),
                    _EXPECTED_CACHE[stage_id],
                )
                self.assertTrue(contract.cache_policy.write_after_validation)
                self.assertTrue(contract.cache_policy.revalidate_hits)
                self.assertEqual(contract.side_effect_policy, _EXPECTED_SIDE_EFFECT[stage_id])

    def test_planning_registration_remains_resolver_only_and_canonical(self) -> None:
        registry = build_l4_registry()
        static_before = registry.stage_ids()
        register_planning_adapter(registry)
        self.assertEqual(registry.stage_ids(), static_before)

        planning = registry.resolve(
            PLANNING_RESOLVER_ID,
            PlanningRegistrationRequest(PlanningRequestKind.EDITORIAL_OUTLINE, expected_count=3),
        )
        self.assertEqual(planning.provider_policy["owner"], "canonical-planning-stage-contract")
        self.assertEqual(planning.retry_policy.owner, "canonical-planning-stage-contract")
        self.assertEqual(planning.implementation_binding.source_path, PLANNING_CONTRACT_SOURCE_PATH)
        self.assertEqual(planning.implementation_binding.source_sha, PLANNING_CONTRACT_SOURCE_SHA)
        self.assertNotIn("planning", registry.stage_ids())

    def test_integrated_install_topology_preserves_planning_media_cinematic_render_order(self) -> None:
        source = inspect.getsource(runtime_closure.install_runtime_closure)
        self._assert_order(
            source,
            (
                "install_runtime_planning_contracts()",
                "install_text_audit_provider_mesh()",
                "install_media_runtime_port()",
                "install_core_reliability_guard()",
                "install_audio_semantic_integrity_binding()",
                "install_audio_mastering_live_binding()",
                "install_cinematic_runtime_port(CinematicInstallPhase.INNER)",
                "install_render_runtime_port()",
                "install_narrative_music_dynamics()",
                "install_canonical_v4_bundle_post_manifest()",
                "install_release_transaction_guard()",
                "install_telemetry_reliability_binding()",
                "sanitize_final_observer_cache_before_runtime()",
                "install_final_qc_observer_durability()",
                "install_audio_semantic_final_gate(production_entrypoint_modules())",
                "install_producer_handoff_contract(production_entrypoint_modules())",
            ),
        )

    def test_integrated_entrypoint_preserves_tts_outer_cinematic_qc_gold_order(self) -> None:
        source = inspect.getsource(run_v3_voice.main)
        self._assert_order(
            source,
            (
                "install_production_model_contract(orchestrator)",
                "install_entrypoint_planning_contracts()",
                "install_runtime_closure()",
                "install_post_runtime_planning_contracts()",
                "install_tts_runtime_port()",
                "install_cinematic_runtime_port(CinematicInstallPhase.OUTER)",
                "install_opening_feasibility_guard()",
                "install_progress_hooks()",
                "orchestrator.produce(",
                "run_final_master_qc(out)",
                "run_gold_enforce_phase4(",
                "run_post_gold_observers(out)",
                "_write_production_manifest(out, production_id=production_id, fmt=plan.format)",
            ),
        )

    def test_progress_hooks_install_after_tts_port_so_it_wraps_voice_mesh_not_the_other_way(self) -> None:
        # scripts/voice_mesh.py::install_voice_mesh() overwrites orchestrator.synthesize_wav
        # unconditionally (it does not compose with whatever was installed before it) while
        # scripts/telegram_progress.py::install_progress_hooks() captures the current
        # orchestrator.synthesize_wav and wraps it. If install_progress_hooks() ever ran
        # before install_tts_runtime_port() (which installs Voice Mesh), Voice Mesh's blind
        # overwrite would silently erase the progress wrapper with no error - exactly the
        # "whichever installs last wins" hazard already found in Planning. This test fails
        # loudly the moment that relative order is ever changed in main().
        source = inspect.getsource(run_v3_voice.main)
        tts_port_pos = source.find("install_tts_runtime_port()")
        progress_hooks_pos = source.find("install_progress_hooks()")
        self.assertGreater(tts_port_pos, -1, "install_tts_runtime_port() call not found")
        self.assertGreater(progress_hooks_pos, -1, "install_progress_hooks() call not found")
        self.assertLess(
            tts_port_pos,
            progress_hooks_pos,
            "install_tts_runtime_port() (which installs Voice Mesh) must run before "
            "install_progress_hooks(), or Voice Mesh's blind overwrite of "
            "orchestrator.synthesize_wav silently discards the progress wrapper",
        )

    def test_short_control_path_preserves_prepare_voice_qc_gold_finalize_order(self) -> None:
        source = inspect.getsource(run_control_production.execute_control_request)
        self._assert_order(
            source,
            (
                "prepare_authoritative_short_for_gold(",
                "run_final_master_qc=production.run_final_master_qc",
                "result = original_gold(**kwargs)",
                "finalize_short_quality(Path(kwargs[\"output_dir\"]), runtime_request, short_pre)",
            ),
        )

    def test_tts_port_fails_closed_when_required_owner_does_not_install(self) -> None:
        def base_boundary(*_args, **_kwargs):
            return None

        def wrong_cloud(*_args, **_kwargs):
            return None

        with patch.object(orchestrator, "_synthesize_tts_section", base_boundary), patch.object(
            orchestrator, "synthesize_wav", wrong_cloud
        ), patch.object(tts_port.voice_mesh, "install_voice_mesh", return_value=None) as installer:
            with self.assertRaisesRegex(tts_port.TTSRuntimePortError, "Voice Mesh cloud boundary"):
                tts_port.install_tts_runtime_port()
        installer.assert_called_once_with()

    def test_media_port_fails_closed_before_downstream_caches(self) -> None:
        with patch.object(media_port.provider_capacity_v2, "_INSTALLED", False), patch.object(
            media_port.provider_capacity_v2, "install_provider_capacity_v2", return_value=None
        ) as installer, patch.object(
            media_port.media_trust_boundary_v2, "install_media_trust_boundary_v2"
        ) as downstream:
            with self.assertRaisesRegex(media_port.MediaRuntimePortError, "Provider Capacity V2"):
                media_port.install_media_runtime_port()
        installer.assert_called_once_with()
        downstream.assert_not_called()

    def test_cinematic_port_fails_closed_on_missing_inner_wrapper_marker(self) -> None:
        def bare_produce(*_args, **_kwargs):
            return None

        with patch.object(orchestrator, "produce", bare_produce), patch.object(
            cinematic_port.sfx_live_binding, "install_sfx_live_binding", return_value=None
        ) as installer:
            with self.assertRaisesRegex(cinematic_port.CinematicRuntimePortError, "SFX live binding"):
                cinematic_port.install_cinematic_runtime_port(cinematic_port.CinematicInstallPhase.INNER)
        installer.assert_called_once_with()

    def test_render_port_fails_closed_when_configured_owner_does_not_install(self) -> None:
        with patch.object(render_port.render_durable_cache, "_shared_root", return_value=Path("/configured")), patch.object(
            render_port.render_durable_cache, "_INSTALLED", False
        ), patch.object(
            render_port.render_durable_cache, "install_render_durable_cache", return_value=None
        ) as installer:
            with self.assertRaisesRegex(render_port.RenderRuntimePortError, "configured Render durable cache"):
                render_port.install_render_runtime_port()
        installer.assert_called_once_with()

    def test_qc_port_propagates_exact_failure_once_without_retry(self) -> None:
        injected = RuntimeError("injected-qc-failure")
        with patch.object(qc_port.core, "run_final_master_qc", side_effect=injected) as core:
            with self.assertRaises(RuntimeError) as caught:
                qc_port.run_final_master_qc(Path("output/fake"))
        self.assertIs(caught.exception, injected)
        core.assert_called_once_with(Path("output/fake"))

    def test_shorts_port_propagates_prepare_and_finalize_failures_once_without_retry(self) -> None:
        request = {"kind": "short"}
        pre_gold = {"stage": "pre_gold"}
        prepare_failure = RuntimeError("injected-short-prepare-failure")
        finalize_failure = RuntimeError("injected-short-finalize-failure")

        with patch.object(shorts_port.core, "prepare_short_render", side_effect=prepare_failure) as prepare:
            with self.assertRaises(RuntimeError) as caught_prepare:
                shorts_port.prepare_short_render(Path("output/fake"), request)
        self.assertIs(caught_prepare.exception, prepare_failure)
        prepare.assert_called_once_with(Path("output/fake"), request)

        with patch.object(shorts_port.core, "finalize_short_quality", side_effect=finalize_failure) as finalize:
            with self.assertRaises(RuntimeError) as caught_finalize:
                shorts_port.finalize_short_quality(Path("output/fake"), request, pre_gold)
        self.assertIs(caught_finalize.exception, finalize_failure)
        finalize.assert_called_once_with(Path("output/fake"), request, pre_gold)

    def test_stable_ports_do_not_add_network_sleep_or_subprocess_execution(self) -> None:
        forbidden = ("requests.", "httpx.", "urllib.", "time.sleep", "subprocess.run", "subprocess.Popen")
        for stage_id, (_port, path) in _PORTS.items():
            source = path.read_text(encoding="utf-8")
            with self.subTest(stage=stage_id):
                for token in forbidden:
                    self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
