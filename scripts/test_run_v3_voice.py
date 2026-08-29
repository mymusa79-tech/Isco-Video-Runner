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


def _gold_success(*_args, **_kwargs):
    return (
        SimpleNamespace(format="film"),
        {"status": "pass", "hard_blocks": []},
        {"phase": "4", "mode": "enforce", "release_authority": "gold"},
    )


class ResolvePlanSourceTests(unittest.TestCase):
    def test_fallback_wins_even_if_providers_were_also_recorded(self) -> None:
        with patch.object(run_v3_voice, "was_fallback_used", return_value=True), patch.object(
            run_v3_voice, "get_used_providers", return_value=["gemini"]
        ):
            self.assertEqual(run_v3_voice._resolve_plan_source(), "product_proof_fallback")

    def test_single_provider(self) -> None:
        with patch.object(run_v3_voice, "was_fallback_used", return_value=False), patch.object(
            run_v3_voice, "get_used_providers", return_value=["gemini"]
        ):
            self.assertEqual(run_v3_voice._resolve_plan_source(), "gemini")

    def test_multiple_providers_are_joined(self) -> None:
        with patch.object(run_v3_voice, "was_fallback_used", return_value=False), patch.object(
            run_v3_voice, "get_used_providers", return_value=["gemini", "groq"]
        ):
            self.assertEqual(run_v3_voice._resolve_plan_source(), "gemini+groq")

    def test_no_providers_recorded_is_unknown(self) -> None:
        with patch.object(run_v3_voice, "was_fallback_used", return_value=False), patch.object(
            run_v3_voice, "get_used_providers", return_value=[]
        ):
            self.assertEqual(run_v3_voice._resolve_plan_source(), "unknown")


class TagPlanSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_tags_both_plan_and_quality_json_without_losing_fields(self) -> None:
        (self.out_dir / "plan.json").write_text(json.dumps({"topic": "x"}), encoding="utf-8")
        (self.out_dir / "quality-final.json").write_text(json.dumps({"duration_ok": True}), encoding="utf-8")
        with patch.object(run_v3_voice, "was_fallback_used", return_value=False), patch.object(
            run_v3_voice, "get_used_providers", return_value=["groq"]
        ):
            run_v3_voice._tag_plan_source(self.out_dir)
        plan = json.loads((self.out_dir / "plan.json").read_text(encoding="utf-8"))
        quality = json.loads((self.out_dir / "quality-final.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["plan_source"], "groq")
        self.assertEqual(quality["plan_source"], "groq")
        self.assertEqual(plan["topic"], "x")
        self.assertTrue(quality["duration_ok"])

    def test_missing_artifact_is_skipped(self) -> None:
        (self.out_dir / "plan.json").write_text(json.dumps({"topic": "x"}), encoding="utf-8")
        with patch.object(run_v3_voice, "was_fallback_used", return_value=True), patch.object(
            run_v3_voice, "get_used_providers", return_value=[]
        ):
            run_v3_voice._tag_plan_source(self.out_dir)
        plan = json.loads((self.out_dir / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["plan_source"], "product_proof_fallback")
        self.assertFalse((self.out_dir / "quality-final.json").exists())


class _chdir:
    def __init__(self, path):
        self._path = path

    def __enter__(self):
        self._original = os.getcwd()
        os.chdir(self._path)
        return self

    def __exit__(self, *exc_info):
        os.chdir(self._original)


class LatestOutputDirTests(unittest.TestCase):
    def test_returns_none_when_no_output_directories_exist(self) -> None:
        with tempfile.TemporaryDirectory() as d, _chdir(d):
            self.assertIsNone(run_v3_voice._latest_output_dir())

    def test_returns_the_most_recently_modified_directory(self) -> None:
        with tempfile.TemporaryDirectory() as d, _chdir(d):
            Path("output").mkdir()
            older = Path("output/older")
            newer = Path("output/newer")
            older.mkdir()
            newer.mkdir()
            now = time.time()
            os.utime(older, (now - 100, now - 100))
            os.utime(newer, (now, now))
            self.assertEqual(run_v3_voice._latest_output_dir(), newer)


class ProductionManifestTests(unittest.TestCase):
    def test_manifest_hashes_final_and_declares_gold_authority(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / "final.mp4").write_bytes(b"immutable-final")
            with patch.dict(
                os.environ,
                {"GITHUB_RUN_ID": "123", "GITHUB_RUN_NUMBER": "77", "GITHUB_RUN_ATTEMPT": "2"},
                clear=False,
            ):
                manifest = run_v3_voice._write_production_manifest(out, production_id="v4:123:2", fmt="film")
            self.assertEqual(manifest["release_authority"], "gold_enforced")
            self.assertEqual(manifest["production_id"], "v4:123:2")
            self.assertEqual(manifest["release_tag"], "video-77")
            self.assertEqual(len(manifest["final_sha256"]), 64)
            self.assertEqual(manifest["publication_binding"], "unbound")

    def test_verified_publication_binding_requires_video_id_and_source(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / "final.mp4").write_bytes(b"final")
            env = {
                "ISCO_PRODUCTION_VIDEO_ID": "abc123",
                "ISCO_PRODUCTION_BINDING_SOURCE": "youtube_upload",
            }
            with patch.dict(os.environ, env, clear=False):
                manifest = run_v3_voice._write_production_manifest(out, production_id="v4:x:1", fmt="film")
            self.assertEqual(manifest["publication_binding"], "verified")
            self.assertEqual(manifest["youtube_video_id"], "abc123")


class _MainPatchMixin:
    def setUp(self) -> None:
        # main() now enforces the exact V4 production model contract before any
        # provider work. Give these flow tests the same explicit environment as the
        # real workflow rather than bypassing or mocking that guard.
        self._model_env = patch.dict(
            os.environ,
            {
                "GEMINI_CONTENT_MODEL": "gemini-3.7-flash",
                "GEMINI_TTS_MODEL": "gemini-3.1-flash-tts-preview",
            },
            clear=False,
        )
        self._model_env.start()
        self.addCleanup(self._model_env.stop)
        original_content_models = set(run_v3_voice.orchestrator.FREE_CONTENT_MODELS)
        original_tts_models = set(run_v3_voice.orchestrator.FREE_TTS_MODELS)
        self.addCleanup(
            lambda: setattr(run_v3_voice.orchestrator, "FREE_CONTENT_MODELS", original_content_models)
        )
        self.addCleanup(
            lambda: setattr(run_v3_voice.orchestrator, "FREE_TTS_MODELS", original_tts_models)
        )

        names = [
            "install_schema_guard",
            "install_router",
            "install_planner_quality_guard",
            "install_attempt9_schema_normalizer",
            "install_append_retry_guard",
            "install_runtime_closure",
            "install_brand_anchor_guard",
            "install_product_proof_fallback",
            "install_voice_mesh",
            "install_voice_identity_observer",
            "install_m7_live_binding",
            "start_progress",
            "install_progress_hooks",
        ]
        self._installers = [patch.object(run_v3_voice, name) for name in names]
        for item in self._installers:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in self._installers])

        def fake_secret(name: str) -> str:
            return {"GEMINI_API_KEY": "gemini-key", "PEXELS_API_KEY": "pexels-key"}.get(name, "")

        self._secret = patch.object(run_v3_voice, "secret", side_effect=fake_secret)
        self._secret.start()
        self.addCleanup(self._secret.stop)
        self._analytics = patch.object(run_v3_voice, "collect_latest_video_metrics_from_env")
        self.analytics = self._analytics.start()
        self.addCleanup(self._analytics.stop)


class MainPhase4FlowTests(_MainPatchMixin, unittest.TestCase):
    def test_success_runs_qc_before_one_gold_enforcer_then_writes_gold_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            request_path = Path(d) / "request.json"
            request_path.write_text(json.dumps({"topic": "x", "format": "film"}), encoding="utf-8")
            out_dir = Path(d) / "output" / "run-1"
            out_dir.mkdir(parents=True)
            (out_dir / "final.mp4").write_bytes(b"fake-final")
            calls: list = []

            def qc(out):
                calls.append(("qc", out))
                return {"status": "pass"}

            def gold(**kwargs):
                calls.append(("gold", kwargs["output_dir"]))
                self.assertEqual(kwargs["gemini"], "gemini-key")
                self.assertEqual(kwargs["pexels"], "pexels-key")
                return _gold_success()

            with patch.dict(os.environ, {"REQUEST_FILE": str(request_path)}, clear=False), patch.object(
                run_v3_voice.orchestrator, "produce", return_value=out_dir
            ) as produce, patch.object(run_v3_voice, "run_final_master_qc", side_effect=qc) as master_qc, patch.object(
                run_v3_voice, "run_gold_enforce_phase4", side_effect=gold
            ) as enforcer, patch.object(
                run_v3_voice, "_tag_plan_source", side_effect=lambda o: calls.append(("tag", o))
            ), patch.object(
                run_v3_voice,
                "write_planning_telemetry",
                side_effect=lambda o: _write_fake_telemetry(o, calls),
            ):
                run_v3_voice.main()

            produce.assert_called_once()
            master_qc.assert_called_once_with(out_dir)
            enforcer.assert_called_once()
            self.assertEqual(calls, [("tag", out_dir), ("qc", out_dir), ("gold", out_dir), ("telemetry", out_dir)])
            telemetry = json.loads((out_dir / "planning-telemetry.json").read_text(encoding="utf-8"))
            self.assertEqual(telemetry["final_critic"]["status"], "pass")
            self.assertEqual(telemetry["gold_enforce_report"]["release_authority"], "gold")
            self.assertEqual(telemetry["production_manifest"]["release_authority"], "gold_enforced")
            self.assertTrue((out_dir / "ai-budget.json").exists())
            self.analytics.assert_called_once()

    def test_core_failure_never_invokes_qc_or_gold_and_still_flushes_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            request_path = Path(d) / "request.json"
            request_path.write_text(json.dumps({"topic": "x", "format": "film"}), encoding="utf-8")
            with _chdir(d):
                Path("output").mkdir()
                out_dir = Path("output/run-1")
                out_dir.mkdir()
                calls = []
                with patch.dict(os.environ, {"REQUEST_FILE": str(request_path)}, clear=False), patch.object(
                    run_v3_voice.orchestrator, "produce", side_effect=RuntimeError("render failed")
                ), patch.object(run_v3_voice, "run_final_master_qc") as master_qc, patch.object(
                    run_v3_voice, "run_gold_enforce_phase4"
                ) as enforcer, patch.object(
                    run_v3_voice, "write_planning_telemetry", side_effect=lambda o: calls.append(o) or _write_fake_telemetry(o)
                ):
                    with self.assertRaisesRegex(RuntimeError, "render failed"):
                        run_v3_voice.main()
                master_qc.assert_not_called()
                enforcer.assert_not_called()
                self.assertEqual(calls, [out_dir])
                self.analytics.assert_not_called()

    def test_qc_failure_is_authoritative_and_prevents_gold_manifest_and_analytics(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            request_path = Path(d) / "request.json"
            request_path.write_text(json.dumps({"topic": "x", "format": "film"}), encoding="utf-8")
            out_dir = Path(d) / "output" / "run-1"
            out_dir.mkdir(parents=True)
            (out_dir / "final.mp4").write_bytes(b"fake-final")
            with patch.dict(os.environ, {"REQUEST_FILE": str(request_path)}, clear=False), patch.object(
                run_v3_voice.orchestrator, "produce", return_value=out_dir
            ), patch.object(
                run_v3_voice, "run_final_master_qc", side_effect=RuntimeError("master qc blocked")
            ) as master_qc, patch.object(run_v3_voice, "run_gold_enforce_phase4") as enforcer, patch.object(
                run_v3_voice, "_tag_plan_source"
            ), patch.object(
                run_v3_voice, "write_planning_telemetry", side_effect=lambda o: _write_fake_telemetry(o)
            ):
                with self.assertRaisesRegex(RuntimeError, "master qc blocked"):
                    run_v3_voice.main()
            master_qc.assert_called_once_with(out_dir)
            enforcer.assert_not_called()
            self.assertTrue((out_dir / "ai-budget.json").exists())
            self.assertTrue((out_dir / "planning-telemetry.json").exists())
            self.assertFalse((out_dir / "production-manifest.json").exists())
            self.analytics.assert_not_called()

    def test_gold_failure_is_authoritative_and_prevents_manifest_and_analytics(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            request_path = Path(d) / "request.json"
            request_path.write_text(json.dumps({"topic": "x", "format": "film"}), encoding="utf-8")
            out_dir = Path(d) / "output" / "run-1"
            out_dir.mkdir(parents=True)
            (out_dir / "final.mp4").write_bytes(b"fake-final")
            with patch.dict(os.environ, {"REQUEST_FILE": str(request_path)}, clear=False), patch.object(
                run_v3_voice.orchestrator, "produce", return_value=out_dir
            ), patch.object(run_v3_voice, "run_final_master_qc", return_value={"status": "pass"}) as master_qc, patch.object(
                run_v3_voice, "run_gold_enforce_phase4", side_effect=RuntimeError("gold blocked")
            ), patch.object(run_v3_voice, "_tag_plan_source"), patch.object(
                run_v3_voice, "write_planning_telemetry", side_effect=lambda o: _write_fake_telemetry(o)
            ):
                with self.assertRaisesRegex(RuntimeError, "gold blocked"):
                    run_v3_voice.main()
            master_qc.assert_called_once_with(out_dir)
            self.assertTrue((out_dir / "ai-budget.json").exists())
            self.assertTrue((out_dir / "planning-telemetry.json").exists())
            self.assertFalse((out_dir / "production-manifest.json").exists())
            self.analytics.assert_not_called()

    def test_core_failure_before_output_directory_preserves_original_exception(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            request_path = Path(d) / "request.json"
            request_path.write_text(json.dumps({"topic": "x", "format": "film"}), encoding="utf-8")
            with _chdir(d), patch.dict(os.environ, {"REQUEST_FILE": str(request_path)}, clear=False), patch.object(
                run_v3_voice.orchestrator, "produce", side_effect=RuntimeError("boom")
            ), patch.object(run_v3_voice, "write_planning_telemetry") as write:
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    run_v3_voice.main()
                write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
