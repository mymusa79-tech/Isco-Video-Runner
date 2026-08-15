from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.run_v3_voice as run_v3_voice


class ResolvePlanSourceTests(unittest.TestCase):
    """Covers item 1: plan_source must always name the real planner that produced a
    run's plan (gemini | groq | openrouter | product_proof_fallback), and must never
    claim a live provider succeeded when the fallback actually produced the plan."""

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
    """Covers item 1: plan_source must land in every JSON artifact the workflow
    uploads (plan.json, quality-final.json), not just one of them."""

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
        # Original fields must survive the round-trip untouched.
        self.assertEqual(plan["topic"], "x")
        self.assertTrue(quality["duration_ok"])

    def test_missing_artifact_is_skipped_not_an_error(self) -> None:
        (self.out_dir / "plan.json").write_text(json.dumps({"topic": "x"}), encoding="utf-8")
        # quality-final.json deliberately absent.

        with patch.object(run_v3_voice, "was_fallback_used", return_value=True), \
                patch.object(run_v3_voice, "get_used_providers", return_value=[]):
            run_v3_voice._tag_plan_source(self.out_dir)  # must not raise

        plan = json.loads((self.out_dir / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["plan_source"], "product_proof_fallback")
        self.assertFalse((self.out_dir / "quality-final.json").exists())


class LatestOutputDirTests(unittest.TestCase):
    """Covers write_planning_telemetry()'s only usable target on a failed run: produce()
    raised before returning an out_dir, so main()'s except-handler must locate whatever
    output directory the run was actually writing to some other way."""

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
    """Minimal contextmanager: temporarily chdir, since _latest_output_dir() reads the
    relative "output" directory exactly like main() does at real production runtime."""

    def __init__(self, path):
        self._path = path

    def __enter__(self):
        self._original = os.getcwd()
        os.chdir(self._path)
        return self

    def __exit__(self, *exc_info):
        os.chdir(self._original)


class MainWritesTelemetryTests(unittest.TestCase):
    """Covers the planning-telemetry request's core guarantee: planning-telemetry.json
    must exist after every real production attempt, success or failure alike - it's
    the exact record needed to see which provider failed and why, so it can't be
    conditioned on produce() actually returning."""

    def setUp(self) -> None:
        self._installers = [
            patch.object(run_v3_voice, "install_schema_guard"),
            patch.object(run_v3_voice, "install_router"),
            patch.object(run_v3_voice, "install_product_proof_fallback"),
            patch.object(run_v3_voice, "install_voice_mesh"),
            patch.object(run_v3_voice, "start_progress"),
            patch.object(run_v3_voice, "install_progress_hooks"),
        ]
        for p in self._installers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._installers])

    def test_success_path_writes_telemetry_once_after_tagging(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            request_path = Path(d) / "request.json"
            request_path.write_text(json.dumps({"topic": "x", "format": "explainer"}), encoding="utf-8")
            out_dir = Path(d) / "output" / "run-1"
            out_dir.mkdir(parents=True)

            calls = []
            with patch.dict(os.environ, {"REQUEST_FILE": str(request_path)}, clear=False), \
                    patch.object(run_v3_voice.orchestrator, "produce", return_value=out_dir), \
                    patch.object(run_v3_voice, "_tag_plan_source", side_effect=lambda o: calls.append(("tag", o))), \
                    patch.object(run_v3_voice, "write_planning_telemetry", side_effect=lambda o: calls.append(("telemetry", o))):
                run_v3_voice.main()

            self.assertEqual(calls, [("tag", out_dir), ("telemetry", out_dir)])

    def test_failure_path_still_writes_telemetry_to_the_latest_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            request_path = Path(d) / "request.json"
            request_path.write_text(json.dumps({"topic": "x", "format": "explainer"}), encoding="utf-8")

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
            request_path.write_text(json.dumps({"topic": "x", "format": "explainer"}), encoding="utf-8")

            with _chdir(d):
                with patch.dict(os.environ, {"REQUEST_FILE": str(request_path)}, clear=False), \
                        patch.object(run_v3_voice.orchestrator, "produce", side_effect=RuntimeError("boom")), \
                        patch.object(run_v3_voice, "write_planning_telemetry") as mock_write:
                    with self.assertRaises(RuntimeError):
                        run_v3_voice.main()

                mock_write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
