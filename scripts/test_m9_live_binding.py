from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import m9_live_binding as binding


def _shot(index: int, start: float, end: float, *, role: str = "develop", intent: str = "continue flow", motif: str = "m1") -> dict:
    return {
        "shot_id": f"sh{index:02d}",
        "start_seconds": start,
        "end_seconds": end,
        "transition_type": "hard_cut",
        "transition_intent": intent,
        "continuity_role": role,
        "motif_ids": [motif],
    }


class M9LiveBindingTests(unittest.TestCase):
    def test_planner_is_sparse_semantic_and_never_mutates_m7_transition_type(self) -> None:
        shots = []
        cursor = 0.0
        for index in range(10):
            role = "establish" if index == 0 else "develop"
            shots.append(_shot(index, cursor, cursor + 10.0, role=role))
            cursor += 10.0
        timeline = {"final_cut_visuals": shots}
        before = json.dumps(timeline, sort_keys=True)
        plan = binding.plan_semantic_transitions(timeline)
        self.assertLessEqual(plan["dissolve_count"], 1)  # 9 boundaries * 15% => 1
        self.assertGreaterEqual(plan["hard_cut_count"], 8)
        self.assertTrue(plan["m7_timeline_preserved"])
        self.assertFalse(plan["m7_transition_type_mutated"])
        self.assertEqual(json.dumps(timeline, sort_keys=True), before)
        self.assertTrue(all(shot["transition_type"] == "hard_cut" for shot in shots))
        for boundary in plan["boundaries"]:
            if boundary["boundary_seconds"] < 30.0:
                self.assertEqual(boundary["transition"], "hard_cut")

    def test_contrast_shift_and_ending_guard_force_hard_cut(self) -> None:
        shots = [
            _shot(0, 0, 35, role="establish"),
            _shot(1, 35, 60, role="contrast", intent="semantic turn"),
            _shot(2, 60, 90, role="develop", intent="continue flow"),
            _shot(3, 90, 110, role="develop", intent="continue flow"),
            _shot(4, 110, 120, role="payoff", intent="land"),
            _shot(5, 120, 126, role="payoff", intent="land"),
            _shot(6, 126, 132, role="payoff", intent="land"),
            _shot(7, 132, 138, role="payoff", intent="land"),
        ]
        plan = binding.plan_semantic_transitions({"final_cut_visuals": shots})
        self.assertEqual(plan["boundaries"][0]["transition"], "hard_cut")
        self.assertIn("break", plan["boundaries"][0]["reason"])
        ending = [x for x in plan["boundaries"] if x["boundary_seconds"] > 126]
        self.assertTrue(all(x["transition"] == "hard_cut" for x in ending))

    def test_adjacent_dissolves_are_forbidden(self) -> None:
        shots = []
        cursor = 0.0
        for index in range(15):
            shots.append(_shot(index, cursor, cursor + 10.0, role="develop", intent="continue flow"))
            cursor += 10.0
        plan = binding.plan_semantic_transitions({"final_cut_visuals": shots})
        dissolve_indices = [x["boundary_index"] for x in plan["boundaries"] if x["transition"] == "dissolve"]
        self.assertTrue(all(b - a > 1 for a, b in zip(dissolve_indices, dissolve_indices[1:])))
        self.assertLessEqual(len(dissolve_indices) / 14, 0.15 + 1e-9)

    def test_missing_or_mismatched_timeline_falls_back_to_original_hard_cut_concat(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "picture.mp4"
            calls = []

            def original(inputs, dest):
                calls.append([Path(x) for x in inputs])
                Path(dest).write_bytes(b"ok")
                return Path(dest)

            with patch.object(binding.orchestrator, "concat_video", original):
                with binding.m9_live_scope():
                    binding.orchestrator.concat_video([root / "a.mp4", root / "b.mp4"], output)
            self.assertEqual(len(calls), 1)
            report = json.loads((root / "m9-transitions.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "skipped")

            (root / "visual-timeline.json").write_text(json.dumps({"final_cut_visuals": [_shot(0, 0, 10)]}), encoding="utf-8")
            calls.clear()
            with patch.object(binding.orchestrator, "concat_video", original):
                with binding.m9_live_scope():
                    binding.orchestrator.concat_video([root / "a.mp4", root / "b.mp4"], output)
            self.assertEqual(len(calls), 1)
            report = json.loads((root / "m9-transitions.json").read_text(encoding="utf-8"))
            self.assertEqual(report["reason"], "m7_final_cut_input_count_mismatch")

    def test_pair_grouping_uses_nonadjacent_dissolve_and_final_duration_guard(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = [root / f"{i}.mp4" for i in range(8)]
            for path in paths:
                path.write_bytes(b"x")
            shots = [_shot(i, i * 10.0, (i + 1) * 10.0, role="develop", intent="continue flow") for i in range(8)]
            (root / "visual-timeline.json").write_text(json.dumps({"final_cut_visuals": shots}), encoding="utf-8")
            seen = []

            def original(inputs, dest):
                seen.append([Path(x) for x in inputs])
                Path(dest).write_bytes(b"joined")
                return Path(dest)

            def fake_pair(left, right, dest, *, dissolve_seconds=binding._DISSOLVE_SECONDS):
                Path(dest).write_bytes(b"pair")
                return Path(dest)

            def fake_duration(path):
                return 80.0 if Path(path).name == "picture.mp4" else (20.0 if Path(path).name.startswith("pair-") else 10.0)

            with patch.object(binding.orchestrator, "concat_video", original), patch.object(binding, "_render_pair", side_effect=fake_pair), patch.object(binding, "duration", side_effect=fake_duration):
                with binding.m9_live_scope():
                    result = binding.orchestrator.concat_video(paths, root / "picture.mp4")
            self.assertEqual(result, root / "picture.mp4")
            report = json.loads((root / "m9-transitions.json").read_text(encoding="utf-8"))
            self.assertLessEqual(report["dissolve_count"], 1)
            if report["dissolve_count"]:
                self.assertEqual(len(seen[0]), 7)

    def test_concat_hook_restored_on_failure(self) -> None:
        original = binding.orchestrator.concat_video
        with self.assertRaises(RuntimeError):
            with binding.m9_live_scope():
                raise RuntimeError("boom")
        self.assertIs(binding.orchestrator.concat_video, original)


if __name__ == "__main__":
    unittest.main()
