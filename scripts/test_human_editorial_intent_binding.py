from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import scripts.m7_live_binding as binding


class HumanEditorialIntentRunnerBindingTests(unittest.TestCase):
    def _scene_plan(self) -> dict:
        return {
            "generation": {"status": "ok"},
            "visual_thesis": {
                "statement": "A visual thesis",
                "arc": {
                    "closed": "closed",
                    "friction": "friction",
                    "shift": "shift",
                    "open": "open",
                },
                "continuity_rule": "motif returns only when meaning changes",
                "motifs": [
                    {
                        "motif_id": "m01",
                        "concept": "window",
                        "story_function": "changing perception",
                    }
                ],
                "anti_cliche_avoid": [],
                "hero_shot_scene_id": "sc01",
            },
            "scenes": [
                {
                    "scene_id": "sc01",
                    "beat_id": "b01",
                    "section_id": "s2",
                    "arc_phase": "shift",
                    "scene_job": "reframe",
                    "visual_concept": "window reflection",
                    "subject": "window",
                    "action": "reflection changes",
                    "environment": "quiet room",
                    "mood": "clear",
                    "symbolism": "window as perception",
                    "motif_ids": ["m01"],
                    "continuity_role": "develop",
                    "callback_to_scene_id": None,
                    "anti_cliche_avoid": [],
                    "hero": True,
                }
            ],
        }

    def _timeline(self) -> dict:
        shot = {
            "section_id": "s2",
            "beat_id": "b01",
            "scene_id": "sc01",
            "shot_id": "sh0001",
            "start_seconds": 20.0,
            "end_seconds": 34.0,
            "provider": "pexels",
            "asset_id": 42,
            "transition_type": "hard_cut",
            "selected_asset": {
                "candidate_ref": "pexels:42",
                "provider": "pexels",
                "asset_id": 42,
            },
            "final_cut_audit_reference": {
                "file": "candidate_manifest.json",
                "json_pointer": "/scenes/0/candidates/0",
            },
        }
        return {
            "schema_version": "cinematic.m7.visual_timeline.v2",
            "timeline_mode": "m6_opening_plus_semantic_director_body",
            "authority": "production_renderer",
            "default_transition": "hard_cut",
            "sections": [
                {
                    "section_id": "s2",
                    "start_seconds": 20.0,
                    "end_seconds": 34.0,
                    "beats": [
                        {
                            "beat_id": "b01",
                            "scenes": [
                                {
                                    "scene_id": "sc01",
                                    "start_seconds": 20.0,
                                    "end_seconds": 34.0,
                                    "shots": [dict(shot)],
                                }
                            ],
                        }
                    ],
                }
            ],
            "final_cut_visuals": [dict(shot)],
        }

    def test_scope_enriches_timeline_and_persists_signature_in_history(self) -> None:
        if binding._load_human_editorial_intent() is None:
            self.skipTest("HEI Engine module not installed; pre-HEI no-op is covered separately")

        captured: dict[str, object] = {}
        original_write = binding.engine_m7._write_timeline
        original_append = binding.orchestrator.append_history
        original_recent = binding.recent_videos

        def fake_write(output_dir: Path, timeline: dict) -> None:
            captured["output_dir"] = Path(output_dir)
            captured["timeline"] = timeline

        def fake_append(record: dict):
            captured["history"] = record
            return record

        try:
            binding.engine_m7._write_timeline = fake_write
            binding.orchestrator.append_history = fake_append
            binding.recent_videos = lambda n=6: []
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "scene_plan.json").write_text(
                    json.dumps(self._scene_plan(), ensure_ascii=False),
                    encoding="utf-8",
                )
                with binding._human_editorial_intent_scope():
                    binding.engine_m7._write_timeline(root, self._timeline())
                    binding.orchestrator.append_history({"topic": "test"})

            timeline = captured["timeline"]
            self.assertEqual(
                timeline["human_editorial_intent"]["episode_visual_thesis"]["hero_shot_scene_id"],
                "sc01",
            )
            self.assertEqual(
                timeline["final_cut_visuals"][0]["human_editorial_intent"]["intent"],
                "metaphor",
            )
            signature = timeline["human_editorial_intent"]["episode_diversity"]["visual_structure_signature"]
            self.assertTrue(signature)
            self.assertEqual(captured["history"]["editorial_visual_signature"], signature)
            self.assertEqual(timeline["final_cut_visuals"][0]["start_seconds"], 20.0)
            self.assertEqual(timeline["final_cut_visuals"][0]["end_seconds"], 34.0)
            self.assertEqual(timeline["final_cut_visuals"][0]["transition_type"], "hard_cut")
        finally:
            binding.engine_m7._write_timeline = original_write
            binding.orchestrator.append_history = original_append
            binding.recent_videos = original_recent

    def test_scope_is_safe_noop_when_exact_hei_module_is_absent(self) -> None:
        original_import = binding.import_module
        original_write = binding.engine_m7._write_timeline
        original_append = binding.orchestrator.append_history

        def missing_hei(name: str):
            if name == binding._HUMAN_EDITORIAL_INTENT_MODULE:
                raise ModuleNotFoundError(
                    f"No module named '{binding._HUMAN_EDITORIAL_INTENT_MODULE}'",
                    name=binding._HUMAN_EDITORIAL_INTENT_MODULE,
                )
            return original_import(name)

        try:
            binding.import_module = missing_hei
            with binding._human_editorial_intent_scope():
                self.assertIs(binding.engine_m7._write_timeline, original_write)
                self.assertIs(binding.orchestrator.append_history, original_append)
        finally:
            binding.import_module = original_import

        self.assertIs(binding.engine_m7._write_timeline, original_write)
        self.assertIs(binding.orchestrator.append_history, original_append)

    def test_loader_does_not_hide_missing_transitive_dependency(self) -> None:
        original_import = binding.import_module

        def missing_dependency(name: str):
            raise ModuleNotFoundError(
                "No module named 'editorial_dependency'",
                name="editorial_dependency",
            )

        try:
            binding.import_module = missing_dependency
            with self.assertRaises(ModuleNotFoundError) as caught:
                binding._load_human_editorial_intent()
            self.assertEqual(caught.exception.name, "editorial_dependency")
        finally:
            binding.import_module = original_import

    def test_scope_restores_engine_and_history_hooks(self) -> None:
        original_loader = binding._load_human_editorial_intent
        original_write = binding.engine_m7._write_timeline
        original_append = binding.orchestrator.append_history
        try:
            binding._load_human_editorial_intent = lambda: (lambda timeline, **kwargs: timeline)
            with binding._human_editorial_intent_scope():
                self.assertIsNot(binding.engine_m7._write_timeline, original_write)
                self.assertIsNot(binding.orchestrator.append_history, original_append)
        finally:
            binding._load_human_editorial_intent = original_loader
        self.assertIs(binding.engine_m7._write_timeline, original_write)
        self.assertIs(binding.orchestrator.append_history, original_append)


if __name__ == "__main__":
    unittest.main()
