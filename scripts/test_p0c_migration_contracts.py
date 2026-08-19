from __future__ import annotations

import ast
import inspect
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.resilient_planner as staged
import scripts.gold_single_evaluator_phase3 as gold_phase3
import scripts.gold_thumbnail_budget as thumbnail_budget
import scripts.run_v3_voice as runner
import scripts.task_level_planner_router as router
import scripts.voice_mesh as voice_mesh


def _main_source() -> str:
    return inspect.getsource(runner.main)


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts = [func.attr]
        value = func.value
        while isinstance(value, ast.Attribute):
            parts.append(value.attr)
            value = value.value
        if isinstance(value, ast.Name):
            parts.append(value.id)
        return ".".join(reversed(parts))
    return ""


def _calls_in_main() -> dict[str, list[ast.Call]]:
    tree = ast.parse(_main_source())
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    calls: dict[str, list[ast.Call]] = {}
    for node in ast.walk(main):
        if isinstance(node, ast.Call):
            calls.setdefault(_call_name(node), []).append(node)
    return calls


def _keyword_is_name(call: ast.Call, keyword: str, name: str) -> bool:
    for item in call.keywords:
        if item.arg == keyword and isinstance(item.value, ast.Name) and item.value.id == name:
            return True
    return False


class RunnerMigrationContractFreezeTests(unittest.TestCase):
    """Contracts owned by Runner adapters that Gold Path migration must preserve."""

    def test_all_runtime_adapters_install_before_the_core_production_call(self) -> None:
        source = _main_source()
        production = source.index("orchestrator.produce(")
        installers = (
            "install_schema_guard()",
            "install_router()",
            "install_planner_quality_guard()",
            "install_append_retry_guard()",
            "install_brand_anchor_guard()",
            "install_product_proof_fallback()",
            "install_voice_mesh()",
            "install_voice_identity_observer()",
            "install_progress_hooks()",
        )
        for installer in installers:
            with self.subTest(installer=installer):
                self.assertLess(source.index(installer), production)

    def test_one_runner_ledger_is_forwarded_to_core_and_single_gold_evaluator(self) -> None:
        calls = _calls_in_main()
        core_calls = calls.get("orchestrator.produce", [])
        gold_calls = calls.get("run_gold_single_evaluator_phase3", [])
        self.assertEqual(len(core_calls), 1)
        self.assertEqual(len(gold_calls), 1)
        self.assertEqual(len(calls.get("_run_final_critic", [])), 0)
        self.assertEqual(len(calls.get("run_gold_shadow_phase2b", [])), 0)
        self.assertTrue(_keyword_is_name(core_calls[0], "ledger", "ledger"))
        self.assertTrue(_keyword_is_name(gold_calls[0], "ledger", "ledger"))

    def test_provenance_and_release_evidence_order_is_stable(self) -> None:
        source = _main_source()
        order = [
            "_tag_plan_source(out)",
            "run_gold_single_evaluator_phase3(",
            "_write_production_manifest(",
            "collect_latest_video_metrics_from_env(",
            "_attach_observer_evidence_to_telemetry(",
        ]
        positions = [source.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))

    def test_phase3_gold_evaluator_has_no_production_or_state_mutation_authority(self) -> None:
        source = inspect.getsource(gold_phase3.run_gold_single_evaluator_phase3)
        self.assertNotIn("orchestrator.produce", source)
        self.assertNotIn("mark_production_accepted", source)
        self.assertNotIn("remove_production_record", source)
        self.assertNotIn("sync_state_snapshot", source)
        self.assertIn('"release_authority": "legacy_v4"', source)
        self.assertIn('"single_gold_evaluator": True', source)

    def test_single_gold_evaluator_uses_gold_task_namespace_and_observe_only_mode(self) -> None:
        source = inspect.getsource(gold_phase3.run_gold_single_evaluator_phase3)
        self.assertEqual(source.count("_run_final_critic("), 1)
        self.assertIn('release_mode="observe_only"', source)
        self.assertIn('task_prefix="GOLD_"', source)
        self.assertIn('task_kind="GOLD_FINAL_CRITIC"', source)

    def test_single_gold_evaluator_runs_once_after_the_single_core_render(self) -> None:
        calls = _calls_in_main()
        self.assertEqual(len(calls.get("orchestrator.produce", [])), 1)
        self.assertEqual(len(calls.get("run_gold_single_evaluator_phase3", [])), 1)
        source = _main_source()
        self.assertLess(source.index("orchestrator.produce("), source.index("run_gold_single_evaluator_phase3("))

    def test_analytics_agent_binding_comes_only_from_the_verified_manifest(self) -> None:
        source = _main_source()
        self.assertIn('expected_video_id=manifest.get("youtube_video_id")', source)
        self.assertIn(
            'production_id=production_id if manifest.get("publication_binding") == "verified" else None',
            source,
        )
        self.assertIn('binding_source=manifest.get("binding_source")', source)

    def test_voice_mesh_keeps_both_cloud_and_local_patch_points(self) -> None:
        original_cloud = getattr(orchestrator, "synthesize_wav", None)
        original_local = getattr(orchestrator, "synthesize_local_wav", None)
        try:
            voice_mesh.install_voice_mesh()
            self.assertIs(orchestrator.synthesize_wav, voice_mesh.synthesize)
            self.assertIs(orchestrator.synthesize_local_wav, voice_mesh.synthesize_local_wav)
        finally:
            if original_cloud is None:
                delattr(orchestrator, "synthesize_wav")
            else:
                orchestrator.synthesize_wav = original_cloud
            if original_local is None:
                delattr(orchestrator, "synthesize_local_wav")
            else:
                orchestrator.synthesize_local_wav = original_local

    def test_planner_router_preserves_engine_patch_contract(self) -> None:
        original_json_text = staged.json_text
        original_build_plan = orchestrator.build_plan
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gemini_file = root / "gemini"
            gemini_file.write_text("fake-gemini", encoding="utf-8")
            cache = root / "planning-checkpoint.json"
            with patch.dict(os.environ, {"GEMINI_API_KEY_FILE": str(gemini_file)}, clear=False), \
                 patch.object(router, "CACHE_PATH", cache):
                try:
                    router.install_router()
                    self.assertTrue(getattr(orchestrator.build_plan, "_is_resilient_router", False))
                    self.assertIsNot(staged.json_text, original_json_text)
                    self.assertEqual(orchestrator.build_plan.__module__, router.__name__)
                finally:
                    staged.json_text = original_json_text
                    orchestrator.build_plan = original_build_plan

    def test_runner_consumes_pexels_once_then_reuses_only_the_in_process_value(self) -> None:
        source = _main_source()
        self.assertEqual(source.count('secret("PEXELS_API_KEY")'), 1)
        self.assertIn('os.environ["PEXELS_API_KEY"] = pexels', source)
        gold = source.index("run_gold_single_evaluator_phase3(")
        self.assertIn("pexels=pexels", source[gold:])
        self.assertNotIn('secret("PEXELS_API_KEY")', source[gold:])

    def test_thumbnail_budget_adapter_delegates_without_copying_packaging_logic(self) -> None:
        source = inspect.getsource(thumbnail_budget)
        self.assertIn("thumbnail.build_thumbnail_package", source)
        self.assertIn("_ledger_call(", source)
        self.assertIn("_ledger_call_status(", source)
        self.assertNotIn("def build_thumbnail_package(", source)
        self.assertNotIn("search_photos(", source)
        self.assertNotIn("render_thumbnail(", source)


if __name__ == "__main__":
    unittest.main()
