from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from isco_video_agent.ai_budget import AttemptOutcome, Priority
import scripts.m7_live_binding as binding


class _Runtime:
    @staticmethod
    def apply_m11_overrides(timeline, scene_plan, prepared, credits, **kwargs):
        self_review = kwargs.get("review_fn")
        if not callable(self_review):
            raise AssertionError("M11 review_fn must be wired")
        timeline["final_cut_visuals"][0]["provider"] = "the_met"
        return [Path("archive.mp4")], [{"provider": "the_met"}], {"status": "applied"}


class _Ledger:
    def __init__(self, *, allow: bool = True):
        self.allow = allow
        self.specs = []
        self.attempts = []

    def register_task(self, spec):
        self.specs.append(spec)

    def authorize(self, task_id: str) -> bool:
        return self.allow

    def record_attempt(self, task_id: str, **kwargs):
        self.attempts.append((task_id, kwargs))


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

    def test_review_budget_denial_blocks_enhancement_without_provider_call(self) -> None:
        ledger = _Ledger(allow=False)
        reviewer = binding._m11_review_fn(
            output_dir=Path("."),
            gemini_api_key="present",
            content_model="gemini-2.5-flash",
            ledger=ledger,
        )
        candidate = SimpleNamespace(provider=SimpleNamespace(value="the_met"), object_id="42")
        with patch.object(binding, "make_image_review_preview") as preview, patch.object(
            binding, "audit_image_preview"
        ) as audit:
            result = reviewer(
                Path("unused.jpg"),
                {"body_index": 0, "query": "solitary figure", "director_evidence": "museum artwork"},
                candidate,
            )
        self.assertEqual(result["status"], "block")
        self.assertIn("budget", result["reason"].lower())
        preview.assert_not_called()
        audit.assert_not_called()
        self.assertEqual(len(ledger.specs), 1)
        self.assertIs(ledger.specs[0].priority, Priority.P2)
        self.assertEqual(ledger.specs[0].max_provider_attempts, 1)
        self.assertEqual(ledger.attempts, [])

    def test_review_pass_records_one_success_attempt(self) -> None:
        ledger = _Ledger()
        reviewer = binding._m11_review_fn(
            output_dir=Path("."),
            gemini_api_key="present",
            content_model="gemini-2.5-flash",
            ledger=ledger,
        )
        candidate = SimpleNamespace(provider=SimpleNamespace(value="the_met"), object_id="42")
        with patch.object(binding, "make_image_review_preview"), patch.object(
            binding,
            "audit_image_preview",
            return_value={"status": "pass", "relevance": 0.9, "visual_quality": 0.9},
        ):
            result = reviewer(
                Path("image.jpg"),
                {"body_index": 0, "query": "solitary figure", "director_evidence": "museum artwork"},
                candidate,
            )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(len(ledger.attempts), 1)
        self.assertIs(ledger.attempts[0][1]["outcome"], AttemptOutcome.SUCCESS)

    def test_review_block_records_content_blocked(self) -> None:
        ledger = _Ledger()
        reviewer = binding._m11_review_fn(
            output_dir=Path("."),
            gemini_api_key="present",
            content_model="gemini-2.5-flash",
            ledger=ledger,
        )
        candidate = SimpleNamespace(provider=SimpleNamespace(value="the_met"), object_id="42")
        with patch.object(binding, "make_image_review_preview"), patch.object(
            binding,
            "audit_image_preview",
            return_value={"status": "block", "reason": "cultural risk"},
        ):
            result = reviewer(
                Path("image.jpg"),
                {"body_index": 0, "query": "solitary figure", "director_evidence": "museum artwork"},
                candidate,
            )
        self.assertEqual(result["status"], "block")
        self.assertEqual(len(ledger.attempts), 1)
        self.assertIs(ledger.attempts[0][1]["outcome"], AttemptOutcome.CONTENT_BLOCKED)

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
                with binding._m11_archive_scope(
                    smithsonian_api_key="optional",
                    gemini_api_key="gemini",
                    ledger=_Ledger(),
                ):
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
                self.assertTrue(callable(kwargs.get("review_fn")))
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
