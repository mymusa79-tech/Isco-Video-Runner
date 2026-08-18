from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import scripts.run_v3_voice as run_v3_voice


def _write_fake_telemetry(out_dir: Path, calls: list | None = None) -> Path:
    if calls is not None:
        calls.append(("telemetry", out_dir))
    path = out_dir / "planning-telemetry.json"
    path.write_text(json.dumps({"providers": {}, "attempts": []}), encoding="utf-8")
    return path


class ResolvePlanSourceTests(unittest.TestCase):
    def test_fallback_wins_even_if_providers_were_also_recorded(self) -> None:
        with patch.object(run_v3_voice, "was_fallback_used", return_value=True), \
                patch.object(run_v3_voice, "get_used_providers", return_value=["gemini"]):
            self.assertEqual(run_v3_voice._resolve_plan_source(), "product_proof_fallback")

    def test_single_provider(self) -> None:
        with patch.object(run_v3_voice, "was_fallback_used", return_value=False), \
                patch.object(run_v3_voice, "get_used_providers", return_value=["gemini"]):
            self.assertEqual(run_v3_voice._resolve_plan_source(), "gemini")

    def test_multiple_providers_are_joined(self) -> None:
        with patch.object(run_v3_voice, "was_fallback_used", return_value=False), \
                patch.object(run_v3_voice, "get_used_providers", return_value=["gemini", "groq"]):
            self.assertEqual(run_v3_voice._resolve_plan_source(), "gemini+groq")

    def test_no_providers_recorded_is_unknown_not_a_false_claim(self) -> None:
        with patch.object(run_v3_voice, "was_fallback_used", return_value=False), \
                patch.object(run_v3_voice, "get_used_providers", return_value=[]):
            self.assertEqual(run_v3_voice._resolve_plan_source(), "unknown")


class TagPlanSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_tags_both_plan_and_quality_json(self) -> None:
        (self.out_dir / "plan.json").write_text(json.dumps({"topic": "x"}), encoding="utf-8")
        (self.out_dir / "quality-final.json").write_text(json.dumps({"duration_ok": True}), encoding="utf-8")

        with patch.object(run_v3_voice, "was_fallback_used", return_value=False), \
                patch.object(run_v3_voice, "get_used_providers", return_value=["groq"]):
            run_v3_voice._tag_plan_source(self.out_dir)

        plan = json.loads((self.out_dir / "plan.json").read_text(encoding="utf-8"))
        quality = json.loads((self.out_dir / "quality-final.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["plan_source"], "groq")
        self.assertEqual(quality["plan_source"], "groq")
        self.assertEqual(plan["topic"], "x")
        self.assertTrue(quality["duration_ok"])

    def test_missing_artifact_is_skipped_not_an_error(self) -> None:
        (self.out_dir / "plan.json").write_text(json.dumps({"topic": "x"}), encoding="utf-8")
        with patch.object(run_v3_voice, "was_fallback_used", return_value=True), \
                patch.object(run_v3_voice, "get_used_providers", return_value=[]):
            run_v3_voice._tag_plan_source(self.out_dir)
        plan = json.loads((self.out_dir / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["plan_source"], "product_proof_fallback")
        self.assertFalse((self.out_dir / "quality-final.json").exists())


class LatestOutputDirTests(unittest.TestCase):
    def test_returns_none_when_no_output_directories_exist(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with _chdir(d):
                self.assertIsNone(run_v3_voice._latest_output_dir())

    def test_returns_the_most_recently_modified_directory(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with _chdir(d):
                Path("output").mkdir()
                older = Path("output/2026-01-01-topic-a")
                older.mkdir()
                newer = Path("output/2026-01-01-topic-b")
                newer.mkdir()
                now = time.time()
                os.utime(older, (now - 100, now - 100))
                os.utime(newer, (now, now))
                self.assertEqual(run_v3_voice._latest_output_dir(), newer)


class _chdir:
    def __init__(self, path):
        self._path = path

    def __enter__(self):
        self._original = os.getcwd()
        os.chdir(self._path)
        return self

    def __exit__(self, *exc_info):
        os.chdir(self._original)


class _MainPatchMixin:
    def setUp(self) -> None:
        names = [
            "install_schema_guard",
            "install_router",
            "install_planner_quality_guard",
            "install_append_retry_guard",
            "install_brand_anchor_guard",
            "install_product_proof_fallback",
            "install_voice_mesh",
            "start_progress",
            "install_progress_hooks",
        ]
        self._installers = [patch.object(run_v3_voice, name) for name in names]
        for item in self._installers:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in self._installers])
        self._secret = patch.object(run_v3_voice, "secret", return_value="gemini-key")
        self._secret.start()
        self.addCleanup(self._secret.stop)
        self._plan = patch.object(
            run_v3_voice,
            "_plan_from_json",
            return_value=SimpleNamespace(format="film"),
        )
        self._plan.start()
        self.addCleanup(self._plan.stop)
        self._analytics = patch.object(run_v3_voice, "collect_latest_video_metrics_from_env")
        self._analytics.start()
        self.addCleanup(self._analytics.stop)


class MainWritesTelemetryTests(_MainPatchMixin, unittest.TestCase):
    def test_success_path_writes_telemetry_once_after_tagging(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            request_path = Path(d) / "request.json"
            request_path.write_text(json.dumps({"topic": "x", "format": "film"}), encoding="utf-8")
            out_dir = Path(d) / "output" / "run-1"
            out_dir.mkdir(parents=True)

            calls = []
            with patch.dict(os.environ, {"REQUEST_FILE": str(request_path)}, clear=False), \
                    patch.object(run_v3_voice.orchestrator, "produce", return_value=out_dir), \
                    patch.object(run_v3_voice, "_run_final_critic", return_value={"status": "pass"}), \
                    patch.object(run_v3_voice, "_tag_plan_source", side_effect=lambda o: calls.append(("tag", o))), \
                    patch.object(
                        run_v3_voice,
                        "write_planning_telemetry",
                        side_effect=lambda o: _write_fake_telemetry(o, calls),
                    ):
                run_v3_voice.main()

            self.assertEqual(calls, [("tag", out_dir), ("telemetry", out_dir)])
            telemetry = json.loads((out_dir / "planning-telemetry.json").read_text(encoding="utf-8"))
            self.assertIn("production_manifest", telemetry)
            self.assertIn("final_critic", telemetry)
            self.assertIn("ai_budget", telemetry)

    def test_failure_path_still_writes_telemetry_to_the_latest_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            request_path = Path(d) / "request.json"
            request_path.write_text(json.dumps({"topic": "x", "format": "film"}), encoding="utf-8")

            with _chdir(d):
                Path("output").mkdir()
                out_dir = Path("output/run-1")
                out_dir.mkdir()

                calls = []
                with patch.dict(os.environ, {"REQUEST_FILE": str(request_path)}, clear=False), \
                        patch.object(run_v3_voice.orchestrator, "produce", side_effect=RuntimeError("boom")), \
                        patch.object(run_v3_voice, "write_planning_telemetry", side_effect=lambda o: calls.append(o)):
                    with self.assertRaises(RuntimeError):
                        run_v3_voice.main()

                self.assertEqual(calls, [out_dir])

    def test_failure_path_with_no_output_dir_yet_does_not_crash_and_still_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            request_path = Path(d) / "request.json"
            request_path.write_text(json.dumps({"topic": "x", "format": "film"}), encoding="utf-8")

            with _chdir(d):
                with patch.dict(os.environ, {"REQUEST_FILE": str(request_path)}, clear=False), \
                        patch.object(run_v3_voice.orchestrator, "produce", side_effect=RuntimeError("boom")), \
                        patch.object(run_v3_voice, "write_planning_telemetry") as mock_write:
                    with self.assertRaises(RuntimeError):
                        run_v3_voice.main()
                mock_write.assert_not_called()


class FinalCriticV4AcceptanceTests(_MainPatchMixin, unittest.TestCase):
    # AC2
    def test_orchestrator_failure_never_invokes_final_critic(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            request_path = Path(d) / "request.json"
            request_path.write_text(json.dumps({"topic": "x", "format": "film"}), encoding="utf-8")
            with _chdir(d):
                with patch.dict(os.environ, {"REQUEST_FILE": str(request_path)}, clear=False), \
                        patch.object(run_v3_voice.orchestrator, "produce", side_effect=RuntimeError("render failed")), \
                        patch.object(run_v3_voice, "_run_final_critic") as critic, \
                        patch.object(run_v3_voice, "write_planning_telemetry"):
                    with self.assertRaises(RuntimeError):
                        run_v3_voice.main()
            critic.assert_not_called()

    # AC3 (and Runner-level proof for AC7: block remains non-blocking in V4)
    def test_success_runs_critic_after_produce_and_observe_block_still_completes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            request_path = Path(d) / "request.json"
            request_path.write_text(json.dumps({"topic": "x", "format": "film"}), encoding="utf-8")
            out_dir = Path(d) / "output" / "run-1"
            out_dir.mkdir(parents=True)
            order = []

            def produce(**kwargs):
                order.append("produce")
                return out_dir

            def critic(**kwargs):
                order.append("critic")
                self.assertEqual(kwargs["release_mode"], "observe_only")
                return {"status": "block", "would_block_if_enforced": True}

            with patch.dict(os.environ, {"REQUEST_FILE": str(request_path)}, clear=False), \
                    patch.object(run_v3_voice.orchestrator, "produce", side_effect=produce), \
                    patch.object(run_v3_voice, "_run_final_critic", side_effect=critic), \
                    patch.object(run_v3_voice, "_tag_plan_source"), \
                    patch.object(
                        run_v3_voice,
                        "write_planning_telemetry",
                        side_effect=lambda o: _write_fake_telemetry(o),
                    ):
                run_v3_voice.main()

            self.assertEqual(order, ["produce", "critic"])
            self.assertTrue((out_dir / "ai-budget.json").exists())
            self.assertTrue((out_dir / "production-manifest.json").exists())
            telemetry = json.loads((out_dir / "planning-telemetry.json").read_text(encoding="utf-8"))
            self.assertEqual(telemetry["final_critic"]["status"], "block")
            self.assertEqual(telemetry["production_manifest"]["publication_binding"], "unbound")


if __name__ == "__main__":
    unittest.main()
