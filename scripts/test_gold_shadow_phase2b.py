from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import isco_video_agent.thumbnail as thumbnail
from isco_video_agent.ai_budget import AttemptOutcome, BudgetLedger, Capability
import scripts.gold_shadow_phase2b as phase2b
import scripts.gold_thumbnail_budget as thumb_budget
import scripts.run_v3_voice as runner


class ThumbnailBudgetAdapterTests(unittest.TestCase):
    def test_concept_and_visual_calls_use_distinct_p2_tasks_and_restore_originals(self) -> None:
        ledger = BudgetLedger("film", enforce=True)
        original_json = Mock(return_value={"concepts": []})
        original_audit = Mock(return_value={"status": "pass"})
        with patch.object(thumbnail, "json_text", original_json), patch.object(
            thumbnail, "audit_image_preview", original_audit
        ):
            before_json = thumbnail.json_text
            before_audit = thumbnail.audit_image_preview
            with thumb_budget._budget_thumbnail_provider_calls(ledger=ledger, model="model"):
                thumbnail.json_text("key", "prompt", model="model")
                thumbnail.audit_image_preview(
                    "key",
                    Path("1-preview-2.jpg"),
                    episode_topic="topic",
                    thumbnail_concept="concept",
                    model="model",
                )
                self.assertIsNot(thumbnail.json_text, before_json)
                self.assertIsNot(thumbnail.audit_image_preview, before_audit)
            self.assertIs(thumbnail.json_text, before_json)
            self.assertIs(thumbnail.audit_image_preview, before_audit)

        summary = ledger.to_summary()
        self.assertEqual(summary["provider_attempts"]["total"], 2)
        self.assertEqual(summary["logical_tasks"]["by_priority"]["P2"], 2)
        self.assertEqual(summary["logical_tasks"]["by_kind"]["GOLD_SHADOW_THUMBNAIL_CONCEPTS"], 1)
        self.assertEqual(summary["logical_tasks"]["by_kind"]["GOLD_SHADOW_THUMBNAIL_VISUAL"], 1)

    def test_enforced_p2_budget_denial_happens_before_provider_call(self) -> None:
        ledger = BudgetLedger("film", enforce=True)
        for index in range(34):
            ledger.record_attempt(
                f"PREEXISTING_{index}",
                provider="test",
                requested_model="model",
                resolved_model="model",
                capability=Capability.TEXT,
                outcome=AttemptOutcome.SUCCESS,
            )
        original_json = Mock(return_value={"concepts": []})
        with patch.object(thumbnail, "json_text", original_json):
            with self.assertRaisesRegex(RuntimeError, "AI budget authorization denied"):
                with thumb_budget._budget_thumbnail_provider_calls(ledger=ledger, model="model"):
                    thumbnail.json_text("key", "prompt", model="model")
        original_json.assert_not_called()
        self.assertEqual(ledger.to_summary()["provider_attempts"]["total"], 34)
        self.assertIn("GOLD_SHADOW_THUMBNAIL_CONCEPTS", ledger.to_summary()["p2_skipped"])

    def test_adapter_delegates_to_canonical_thumbnail_builder_without_copying_packaging_logic(self) -> None:
        source = inspect.getsource(thumb_budget)
        self.assertIn("thumbnail.build_thumbnail_package", source)
        self.assertNotIn("def build_thumbnail_package(", source)
        self.assertNotIn("search_photos(", source)
        self.assertNotIn("render_thumbnail(", source)


class Phase2BIsolationTests(unittest.TestCase):
    def _write_release_inputs(self, root: Path) -> None:
        (root / "final.mp4").write_bytes(b"same-render-bytes")
        (root / "plan.json").write_text(json.dumps({"format": "film"}), encoding="utf-8")
        (root / "quality-final.json").write_text(json.dumps({"duration_ok": True}), encoding="utf-8")
        (root / "visual-audit.json").write_text(json.dumps([]), encoding="utf-8")
        (root / "rights-manifest.json").write_text(
            json.dumps({"visuals": [{"provider": "pexels"}]}), encoding="utf-8"
        )
        (root / "monetization-check.json").write_text(json.dumps({"status": "PASS"}), encoding="utf-8")

    def test_shadow_uses_same_render_and_never_mutates_canonical_packaging_or_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "output"
            root.mkdir()
            self._write_release_inputs(root)
            history = Path(tmp) / "history.json"
            history.write_text(json.dumps({"productions": [{"id": 1}]}), encoding="utf-8")
            canonical_rights_before = (root / "rights-manifest.json").read_bytes()
            package = {
                "status": "ready",
                "candidates": [
                    {
                        "photo_provider": "pexels",
                        "photo_id": 7,
                        "photo_url": "https://www.pexels.com/photo/7/",
                        "photographer": "p",
                        "photographer_url": "https://www.pexels.com/@p/",
                        "license_url": "https://www.pexels.com/license/",
                        "retrieved_at": "now",
                        "file": "thumbnail-1.jpg",
                        "visual_audit": {"status": "pass"},
                    }
                ],
            }
            gold_critic = {
                "status": "pass",
                "observation_status": "ok",
                "hard_blocks": [],
                "model_review": {"status": "pass"},
            }
            gold_result = {
                "state_semantics": {
                    "would_accept": True,
                    "would_reject": False,
                    "state_mutation_performed": False,
                }
            }
            ledger = BudgetLedger("film", enforce=True)
            fake_plan = object()
            with patch.object(phase2b, "history_path", return_value=history), patch.object(
                phase2b, "build_budgeted_thumbnail_package", return_value=package
            ), patch.object(
                phase2b, "observe_gold_output", return_value=(fake_plan, gold_critic, gold_result)
            ):
                comparison = phase2b.run_gold_shadow_phase2b(
                    output_dir=root,
                    gemini="g",
                    pexels="p",
                    ledger=ledger,
                    legacy_critic={"status": "pass", "observation_status": "ok", "hard_blocks": []},
                    plan_from_json=lambda _path: fake_plan,
                    run_final_critic=Mock(),
                )

            self.assertFalse(comparison["same_render"]["artifact_divergence"])
            self.assertTrue(comparison["same_render"]["shadow_uses_hard_link"])
            self.assertFalse(comparison["rights_observation"]["canonical_rights_mutation_detected"])
            self.assertFalse(comparison["thumbnail_shadow"]["canonical_package_mutation_detected"])
            self.assertFalse(comparison["state_observation"]["state_mutation_detected"])
            self.assertEqual((root / "rights-manifest.json").read_bytes(), canonical_rights_before)
            self.assertFalse((root / "thumbnail-plan.json").exists())
            shadow_rights = json.loads(
                (root / "gold-shadow" / "phase2b" / "eval-root" / "rights-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(len(shadow_rights["thumbnails"]), 1)


class Phase2BRunnerContracts(unittest.TestCase):
    def test_runner_owns_pexels_secret_once_and_passes_same_ledger_to_shadow(self) -> None:
        source = inspect.getsource(runner.main)
        self.assertEqual(source.count('secret("PEXELS_API_KEY")'), 1)
        self.assertIn('os.environ["PEXELS_API_KEY"] = pexels', source)
        self.assertEqual(source.count("orchestrator.produce("), 1)
        self.assertEqual(source.count("run_gold_shadow_phase2b("), 1)
        shadow = source.index("run_gold_shadow_phase2b(")
        self.assertLess(source.index("orchestrator.produce("), shadow)
        self.assertIn("pexels=pexels", source[shadow:])
        self.assertIn("ledger=ledger", source[shadow:])


if __name__ == "__main__":
    unittest.main()
