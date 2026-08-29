from __future__ import annotations

import hashlib
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import isco_video_agent.orchestrator as orchestrator

from scripts import orchestration_tts_port as port
from scripts import voice_identity_observer, voice_mesh
from scripts.orchestration_stage_registry import build_l4_registry


class TTSStablePortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_section = orchestrator._synthesize_tts_section
        self.original_cloud = orchestrator.synthesize_wav
        self.original_local = orchestrator.synthesize_local_wav
        self.original_observer_inner = voice_identity_observer._original_synthesize_tts_section

        def base_boundary(*_args, **kwargs):
            return kwargs.get("output")

        self.base_boundary = base_boundary
        orchestrator._synthesize_tts_section = base_boundary
        self.calls: list[str] = []

    def tearDown(self) -> None:
        orchestrator._synthesize_tts_section = self.original_section
        orchestrator.synthesize_wav = self.original_cloud
        orchestrator.synthesize_local_wav = self.original_local
        voice_identity_observer._original_synthesize_tts_section = self.original_observer_inner

    def _install_voice_mesh(self) -> None:
        self.calls.append("voice_mesh")
        orchestrator.synthesize_wav = voice_mesh.synthesize
        orchestrator.synthesize_local_wav = voice_mesh.synthesize_local_wav

    def _install_cache(self) -> None:
        self.calls.append("durable_cache")
        if not str(os.environ.get("ISCO_TTS_CACHE_PATH") or "").strip():
            return
        original = orchestrator._synthesize_tts_section

        def cached(*args, **kwargs):
            return original(*args, **kwargs)

        cached._is_tts_durable_cache = True
        cached._tts_durable_cache_original = original
        orchestrator._synthesize_tts_section = cached

    def _install_observer(self) -> None:
        self.calls.append("voice_identity")
        original = orchestrator._synthesize_tts_section
        voice_identity_observer._original_synthesize_tts_section = original

        def observed(*args, **kwargs):
            return original(*args, **kwargs)

        observed._is_voice_identity_observer = True
        orchestrator._synthesize_tts_section = observed

    def _patched_installers(self):
        return (
            patch.object(port.voice_mesh, "install_voice_mesh", side_effect=self._install_voice_mesh),
            patch.object(port.tts_durable_cache, "install_tts_durable_cache", side_effect=self._install_cache),
            patch.object(port.voice_identity_observer, "install_voice_identity_observer", side_effect=self._install_observer),
        )

    def test_port_preserves_historical_order_and_is_idempotent(self) -> None:
        voice_patch, cache_patch, observer_patch = self._patched_installers()
        with patch.dict(os.environ, {"ISCO_TTS_CACHE_PATH": "/tmp/tts-cache"}, clear=False), voice_patch, cache_patch, observer_patch:
            first = port.install_tts_runtime_port()
            first_boundary = orchestrator._synthesize_tts_section
            second = port.install_tts_runtime_port()

        self.assertEqual(self.calls, ["voice_mesh", "durable_cache", "voice_identity"])
        self.assertIs(orchestrator._synthesize_tts_section, first_boundary)
        self.assertEqual(first, second)
        self.assertTrue(first.voice_mesh_installed)
        self.assertTrue(first.durable_cache_configured)
        self.assertTrue(first.durable_cache_installed)
        self.assertTrue(first.voice_identity_observer_installed)
        self.assertEqual(first.provider_owner, "legacy-voice-mesh-core")
        self.assertEqual(first.retry_owner, "legacy-voice-mesh-core")
        self.assertEqual(first.audio_semantic_integrity_owner, "produce-scope-existing-owner")

    def test_cache_remains_optional_when_not_configured(self) -> None:
        voice_patch, cache_patch, observer_patch = self._patched_installers()
        with patch.dict(os.environ, {"ISCO_TTS_CACHE_PATH": ""}, clear=False), voice_patch, cache_patch, observer_patch:
            evidence = port.install_tts_runtime_port()

        self.assertEqual(self.calls, ["voice_mesh", "durable_cache", "voice_identity"])
        self.assertFalse(evidence.durable_cache_configured)
        self.assertFalse(evidence.durable_cache_installed)
        self.assertTrue(evidence.voice_identity_observer_installed)

    def test_configured_cache_missing_from_boundary_fails_closed(self) -> None:
        def cache_noop() -> None:
            self.calls.append("durable_cache")

        with patch.dict(os.environ, {"ISCO_TTS_CACHE_PATH": "/tmp/tts-cache"}, clear=False), patch.object(
            port.voice_mesh, "install_voice_mesh", side_effect=self._install_voice_mesh
        ), patch.object(
            port.tts_durable_cache, "install_tts_durable_cache", side_effect=cache_noop
        ):
            with self.assertRaises(port.TTSRuntimePortError):
                port.install_tts_runtime_port()

        self.assertEqual(self.calls, ["voice_mesh", "durable_cache"])

    def test_stage_registry_binds_tts_to_exact_port_blob_and_preserves_owners(self) -> None:
        contract = build_l4_registry().get("tts")
        binding = contract.implementation_binding
        data = Path("scripts/orchestration_tts_port.py").read_bytes()
        actual_blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()

        self.assertEqual(binding.adapter_id, port.PORT_ID)
        self.assertEqual(binding.source_path, "scripts/orchestration_tts_port.py")
        self.assertEqual(binding.source_sha, actual_blob)
        self.assertEqual(contract.provider_policy["owner"], port.PROVIDER_OWNER)
        self.assertEqual(contract.retry_policy.owner, port.RETRY_OWNER)
        self.assertTrue(contract.retry_policy.bounded)
        self.assertTrue(contract.cache_policy.read)
        self.assertTrue(contract.cache_policy.write)
        self.assertTrue(contract.cache_policy.write_after_validation)
        self.assertTrue(contract.cache_policy.revalidate_hits)

    def test_production_entrypoint_uses_only_the_stable_tts_install_seam(self) -> None:
        source = Path("scripts/run_v3_voice.py").read_text(encoding="utf-8")
        self.assertIn("from scripts.orchestration_tts_port import install_tts_runtime_port", source)
        self.assertEqual(source.count("install_tts_runtime_port()"), 1)
        self.assertNotIn("from scripts.tts_durable_cache import install_tts_durable_cache", source)
        self.assertNotIn("from scripts.voice_identity_observer import install_voice_identity_observer", source)
        self.assertNotIn("from scripts.voice_mesh import install_voice_mesh", source)
        self.assertNotIn("install_tts_durable_cache()", source)
        self.assertNotIn("install_voice_identity_observer()", source)
        self.assertNotIn("install_voice_mesh()", source)

    def test_port_does_not_take_provider_or_retry_execution_ownership(self) -> None:
        source = Path("scripts/orchestration_tts_port.py").read_text(encoding="utf-8")
        for forbidden in ("requests.", "time.sleep", "genai.Client", "gemini_synthesize(", "_local("):
            self.assertNotIn(forbidden, source)
        self.assertIn('PROVIDER_OWNER = "legacy-voice-mesh-core"', source)
        self.assertIn('RETRY_OWNER = "legacy-voice-mesh-core"', source)


if __name__ == "__main__":
    unittest.main()
