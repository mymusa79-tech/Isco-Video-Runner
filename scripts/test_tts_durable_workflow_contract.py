from __future__ import annotations

import inspect
import unittest
from pathlib import Path

import scripts.run_v3_voice as run_v3_voice


WORKFLOW = Path(".github/workflows/produce-resilient-v4.yml")


class TtsDurableWorkflowContractTests(unittest.TestCase):
    def test_cache_restore_precedes_production_and_save_runs_after_failure(self) -> None:
        source = WORKFLOW.read_text(encoding="utf-8")
        restore = source.index("- name: Restore durable TTS section cache")
        produce = source.index("- name: Produce with task-level brain and voice meshes")
        prepare = source.index("- name: Prepare durable TTS section cache for cross-run save")
        save = source.index("- name: Save durable TTS section cache")
        self.assertLess(restore, produce)
        self.assertLess(produce, prepare)
        self.assertLess(prepare, save)
        save_block = source[save:source.index("- name: Prepare Pixabay cache for cross-run save", save)]
        self.assertIn("if: always() && steps.prepare_tts_cache.outputs.save_allowed == 'true'", save_block)

    def test_production_receives_a_dedicated_cache_namespace(self) -> None:
        source = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("ISCO_TTS_CACHE_DIR: ${{ runner.temp }}/isco-tts-section-cache", source)
        self.assertIn("key: tts-section-v1-${{ runner.os }}-${{ github.run_id }}", source)
        self.assertIn("tts-section-v1-${{ runner.os }}-", source)
        self.assertIn("path: ${{ runner.temp }}/isco-tts-section-cache", source)
        self.assertNotEqual("tts-section-v1", "pixabay-search-v2")

    def test_cache_is_installed_between_voice_mesh_and_identity_observer(self) -> None:
        source = inspect.getsource(run_v3_voice.main)
        voice_mesh = source.index("install_voice_mesh()")
        tts_cache = source.index("install_tts_durable_section_cache()")
        identity = source.index("install_voice_identity_observer()")
        self.assertLess(voice_mesh, tts_cache)
        self.assertLess(tts_cache, identity)

    def test_failure_diagnostics_include_cache_resume_evidence(self) -> None:
        source = WORKFLOW.read_text(encoding="utf-8")
        diagnostics = source.index("- name: Upload diagnostics on failure")
        final_review = source.index("- name: Final review and extract result")
        block = source[diagnostics:final_review]
        self.assertIn("engine/output/*/tts-durable-cache-audit.json", block)


if __name__ == "__main__":
    unittest.main()
