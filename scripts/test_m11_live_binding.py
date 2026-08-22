from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.m7_live_binding as binding


class _Runtime:
    @staticmethod
    def apply_m11_overrides(timeline, scene_plan, prepared, credits, **kwargs):
        timeline["final_cut_visuals"][0]["provider"] = "the_met"
        return [Path("archive.mp4")], [{"provider": "the_met"}], {"status": "applied"}


class M11LiveBindingTests(unittest.TestCase):
    def test_missing_engine_runtime_is_strict_noop(self) -> None:
        sentinel = object()
        original = binding.engine_m7.materialize_semantic_body
        with patch.object(binding, "_load_m11_runtime", return_value=None):
            with binding._m11_archive_scope():
                self.assertIs(binding.engine_m7.materialize_semantic_body, original)
                sentinel = binding.engine_m7.materialize_semantic_body
        self.assertIs(sentinel, original)
        self.assertIs(binding.engine_m7.materialize_semantic_body, original)

    def test_applied_override_rewrites_persisted_timeline_after_materialization(self) -> None:
        calls: list[tuple[Path, dict]] = []

        def original_materialize(timeline, **kwargs):
            return [Path("stock.mp4")], [{"provider": "pexels"}], [{"status": "pass"}]

        def write_timeline(out: Path, timeline: dict):
            calls.append((Path(out), dict(timeline["final_cut_visuals"][0])))

        timeline = {
            "duration_seconds": 90.0,
            "final_cut_visuals": [
                {"provider": "pexels", "section_id": "s2", "scene_id": "sc2"}
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "scene_plan.json").write_text('{"scenes": []}', encoding="utf-8")
            with patch.object(binding, "_load_m11_runtime", return_value=_Runtime), patch.object(
                binding.engine_m7, "materialize_semantic_body", original_materialize
            ), patch.object(binding.engine_m7, "_write_timeline", write_timeline):
                with binding._m11_archive_scope(smithsonian_api_key="optional"):
                    prepared, credits, audits = binding.engine_m7.materialize_semantic_body(
                        timeline,
                        out=out,
                        fps=30,
                        first_section_id="s1",
                    )
            self.assertEqual(prepared, [Path("archive.mp4")])
            self.assertEqual(credits[0]["provider"], "the_met")
            self.assertEqual(audits, [{"status": "pass"}])
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], out)
            self.assertEqual(calls[0][1]["provider"], "the_met")

    def test_degraded_stock_does_not_duplicate_timeline_write(self) -> None:
        class Runtime:
            @staticmethod
            def apply_m11_overrides(timeline, scene_plan, prepared, credits, **kwargs):
                return prepared, credits, {"status": "degraded_to_certified_stock"}

        writes: list[int] = []
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "scene_plan.json").write_text('{"scenes": []}', encoding="utf-8")
            with patch.object(binding, "_load_m11_runtime", return_value=Runtime), patch.object(
                binding.engine_m7,
                "materialize_semantic_body",
                lambda timeline, **kwargs: ([Path("stock.mp4")], [{"provider": "pexels"}], []),
            ), patch.object(binding.engine_m7, "_write_timeline", lambda *args: writes.append(1)):
                with binding._m11_archive_scope():
                    binding.engine_m7.materialize_semantic_body(
                        {"final_cut_visuals": []},
                        out=out,
                        fps=30,
                        first_section_id="s1",
                    )
        self.assertEqual(writes, [])


if __name__ == "__main__":
    unittest.main()
