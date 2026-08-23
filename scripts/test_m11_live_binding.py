from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from isco_video_agent.ai_budget import AttemptOutcome, Priority
import scripts.m7_live_binding as binding


class _ReviewModule:
    @staticmethod
    def audit_archive_image(*args, **kwargs):
        return {"status": "pass", "relevance": 0.9, "visual_quality": 0.9}


class _Runtime:
    @staticmethod
    def apply_m11_overrides(timeline, scene_plan, prepared, credits, **kwargs):
        if not callable(kwargs.get("review_fn")):
            raise AssertionError("M11 review_fn must be wired")
        timeline["final_cut_visuals"][0]["provider"] = "the_met"
        return [Path("archive.mp4")], [{"provider": "the_met"}], {"status": "applied"}


class _Ledger:
    def __init__(self, *, allow: bool = True):
        self.allow = allow
        self.specs = []
        self.attempts = []

    def register_task(self, spec): self.specs.append(spec)
    def authorize(self, task_id: str) -> bool: return self.allow
    def record_attempt(self, task_id: str, **kwargs): self.attempts.append((task_id, kwargs))


class M11LiveBindingTests(unittest.TestCase):
    def _reviewer(self, ledger, audit_fn):
        return binding._m11_review_fn(
            output_dir=Path("."), gemini_api_key="present",
            content_model="gemini-2.5-flash", ledger=ledger, audit_fn=audit_fn,
        )

    def test_missing_engine_runtime_is_strict_noop(self) -> None:
        original = binding.engine_m7.materialize_semantic_body
        with patch.object(binding, "_load_m11_runtime", return_value=None):
            with binding._m11_archive_scope():
                self.assertIs(binding.engine_m7.materialize_semantic_body, original)
        self.assertIs(binding.engine_m7.materialize_semantic_body, original)

    def test_review_budget_denial_blocks_without_provider_call(self) -> None:
        ledger = _Ledger(allow=False)
        audit_calls = []
        reviewer = self._reviewer(ledger, lambda *a, **k: audit_calls.append(1))
        candidate = SimpleNamespace(provider=SimpleNamespace(value="the_met"), object_id="42")
        with patch.object(binding, "make_image_review_preview") as preview:
            result = reviewer(Path("unused.jpg"), {"body_index": 0, "director_evidence": "museum artwork"}, candidate)
        self.assertEqual(result["status"], "block")
        preview.assert_not_called()
        self.assertEqual(audit_calls, [])
        self.assertIs(ledger.specs[0].priority, Priority.P2)
        self.assertEqual(ledger.specs[0].max_provider_attempts, 1)
        self.assertEqual(ledger.attempts, [])

    def test_review_pass_records_one_success_attempt(self) -> None:
        ledger = _Ledger()
        reviewer = self._reviewer(
            ledger,
            lambda *a, **k: {"status": "pass", "relevance": 0.9, "visual_quality": 0.9},
        )
        candidate = SimpleNamespace(provider=SimpleNamespace(value="the_met"), object_id="42")
        with patch.object(binding, "make_image_review_preview"):
            result = reviewer(Path("image.jpg"), {"body_index": 0, "director_evidence": "museum artwork"}, candidate)
        self.assertEqual(result["status"], "pass")
        self.assertIs(ledger.attempts[0][1]["outcome"], AttemptOutcome.SUCCESS)

    def test_review_block_records_content_blocked(self) -> None:
        ledger = _Ledger()
        reviewer = self._reviewer(ledger, lambda *a, **k: {"status": "block", "reason": "cultural risk"})
        candidate = SimpleNamespace(provider=SimpleNamespace(value="the_met"), object_id="42")
        with patch.object(binding, "make_image_review_preview"):
            result = reviewer(Path("image.jpg"), {"body_index": 0, "director_evidence": "museum artwork"}, candidate)
        self.assertEqual(result["status"], "block")
        self.assertIs(ledger.attempts[0][1]["outcome"], AttemptOutcome.CONTENT_BLOCKED)

    def test_applied_override_rewrites_persisted_timeline(self) -> None:
        calls = []
        timeline = {"duration_seconds": 90.0, "final_cut_visuals": [{"provider": "pexels", "section_id": "s2", "scene_id": "sc2"}]}
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "scene_plan.json").write_text('{"scenes": []}', encoding="utf-8")
            with patch.object(binding, "_load_m11_runtime", return_value=_Runtime), patch.object(
                binding, "import_module", side_effect=lambda name: _ReviewModule if name == binding._M11_REVIEW_MODULE else __import__(name, fromlist=['*'])
            ), patch.object(
                binding.engine_m7, "materialize_semantic_body",
                lambda timeline, **kwargs: ([Path("stock.mp4")], [{"provider": "pexels"}], [{"status": "pass"}]),
            ), patch.object(binding.engine_m7, "_write_timeline", lambda out, tl: calls.append(dict(tl["final_cut_visuals"][0]))):
                with binding._m11_archive_scope(gemini_api_key="gemini", ledger=_Ledger()):
                    prepared, credits, audits = binding.engine_m7.materialize_semantic_body(
                        timeline, out=out, fps=30, first_section_id="s1"
                    )
        self.assertEqual(prepared, [Path("archive.mp4")])
        self.assertEqual(credits[0]["provider"], "the_met")
        self.assertEqual(audits, [{"status": "pass"}])
        self.assertEqual(calls[0]["provider"], "the_met")

    def test_degraded_stock_does_not_duplicate_timeline_write(self) -> None:
        class Runtime:
            @staticmethod
            def apply_m11_overrides(timeline, scene_plan, prepared, credits, **kwargs):
                return prepared, credits, {"status": "degraded_to_certified_stock"}
        writes = []
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "scene_plan.json").write_text('{"scenes": []}', encoding="utf-8")
            with patch.object(binding, "_load_m11_runtime", return_value=Runtime), patch.object(
                binding, "import_module", side_effect=lambda name: _ReviewModule if name == binding._M11_REVIEW_MODULE else __import__(name, fromlist=['*'])
            ), patch.object(
                binding.engine_m7, "materialize_semantic_body",
                lambda timeline, **kwargs: ([Path("stock.mp4")], [{"provider": "pexels"}], []),
            ), patch.object(binding.engine_m7, "_write_timeline", lambda *args: writes.append(1)):
                with binding._m11_archive_scope():
                    binding.engine_m7.materialize_semantic_body({"final_cut_visuals": []}, out=out, fps=30, first_section_id="s1")
        self.assertEqual(writes, [])


if __name__ == "__main__":
    unittest.main()
