from __future__ import annotations

import hashlib
import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import resume_gold_qc_pending_v1 as resume


SOURCE_RUNNER_SHA = "a" * 40
SOURCE_ENGINE_SHA = "b" * 40
RUNTIME_RUNNER_SHA = "c" * 40
RUNTIME_ENGINE_SHA = "d" * 40
RUN_ID = "99123"
OUTPUT_KEY = "output/resume/final.mp4"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ResumeGoldQCPendingV1Tests(unittest.TestCase):
    def _fixture(self):
        parent = Path(tempfile.mkdtemp(prefix="gold-resume-v1-"))
        bundle = parent / "bundle"
        bundle.mkdir()
        (bundle / "final.mp4").write_bytes(b"exact-final")
        checkpoint = {
            "format": "film",
            "production_state": {
                "output_key": OUTPUT_KEY,
                "record": {
                    "created_at": "2026-09-07T20:00:00+00:00",
                    "topic": "resume",
                    "format": "film",
                    "output": OUTPUT_KEY,
                },
            },
        }
        (bundle / "qc-pending.json").write_text(json.dumps(checkpoint), encoding="utf-8")
        durable = parent / "durable-history.json"
        durable.write_text(json.dumps({"videos": [{"output": "output/other/final.mp4"}]}), encoding="utf-8")
        accepted = parent / "accepted-history.json"
        result = parent / "result.json"
        manifest = {
            "source": {"run_id": RUN_ID, "run_attempt": "1"},
            "final_sha256": _sha(bundle / "final.mp4"),
        }
        return parent, bundle, durable, accepted, result, manifest

    def _execute_kwargs(self, bundle, durable, accepted, result):
        return dict(
            bundle_dir=bundle,
            durable_history=durable,
            accepted_history_output=accepted,
            result_output=result,
            expected_source_run_id=RUN_ID,
            expected_source_runner_sha=SOURCE_RUNNER_SHA,
            expected_source_engine_sha=SOURCE_ENGINE_SHA,
            expected_runtime_runner_sha=RUNTIME_RUNNER_SHA,
            expected_runtime_engine_sha=RUNTIME_ENGINE_SHA,
        )

    def test_success_uses_temp_history_and_exports_only_gold_accepted_copy(self) -> None:
        _, bundle, durable, accepted, result, manifest = self._fixture()
        durable_before = durable.read_bytes()

        def fake_gold(*, output_dir, gemini, pexels, pixabay, ledger):
            del output_dir, gemini, pexels, pixabay, ledger
            temp = Path(os.environ["ISCO_HISTORY_PATH"])
            data = json.loads(temp.read_text(encoding="utf-8"))
            row = next(item for item in data["videos"] if item.get("output") == OUTPUT_KEY)
            row["release_status"] = "accepted_after_final_critic"
            temp.write_text(json.dumps(data), encoding="utf-8")
            plan = type("Plan", (), {"format": "film"})()
            critic = {"status": "pass", "hard_blocks": []}
            report = {
                "gold": {"accepted": True},
                "viewer_quality": {"verdict": "pass", "score_10": 9.1},
            }
            return plan, critic, report

        with patch.dict(os.environ, {"GITHUB_SHA": RUNTIME_RUNNER_SHA}, clear=False), patch.object(
            resume, "validate_resume_bundle", return_value=manifest
        ), patch.object(
            resume, "_git_head", side_effect=[RUNTIME_RUNNER_SHA, RUNTIME_ENGINE_SHA]
        ), patch.object(resume, "_output_key", return_value=OUTPUT_KEY), patch.object(
            resume, "secret", side_effect=lambda name: "key" if name in {"GEMINI_API_KEY", "PEXELS_API_KEY"} else ""
        ), patch.object(resume, "install_production_model_contract"), patch.object(
            resume, "install_runtime_closure"
        ), patch.object(resume, "install_gold_vision_capacity_reserve_v1"), patch.object(
            resume, "run_gold_enforce_phase4", side_effect=fake_gold
        ):
            outcome = resume.execute_gold_resume(**self._execute_kwargs(bundle, durable, accepted, result))

        self.assertEqual(durable.read_bytes(), durable_before)
        self.assertTrue(accepted.is_file())
        accepted_data = json.loads(accepted.read_text(encoding="utf-8"))
        matches = [row for row in accepted_data["videos"] if row.get("output") == OUTPUT_KEY]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["release_status"], "accepted_after_final_critic")
        self.assertFalse(outcome["planning_performed"])
        self.assertFalse(outcome["rerender_performed"])
        self.assertFalse(outcome["durable_history_persisted_by_executor"])
        self.assertTrue(outcome["source_and_runtime_both_certified"])
        self.assertEqual(outcome["source_runner_sha"], SOURCE_RUNNER_SHA)
        self.assertEqual(outcome["runtime_runner_sha"], RUNTIME_RUNNER_SHA)
        self.assertEqual(_sha(bundle / "final.mp4"), manifest["final_sha256"])

    def test_gold_failure_never_changes_durable_history_or_exports_accepted_copy(self) -> None:
        _, bundle, durable, accepted, result, manifest = self._fixture()
        durable_before = durable.read_bytes()
        with patch.dict(os.environ, {"GITHUB_SHA": RUNTIME_RUNNER_SHA}, clear=False), patch.object(
            resume, "validate_resume_bundle", return_value=manifest
        ), patch.object(
            resume, "_git_head", side_effect=[RUNTIME_RUNNER_SHA, RUNTIME_ENGINE_SHA]
        ), patch.object(resume, "_output_key", return_value=OUTPUT_KEY), patch.object(
            resume, "secret", return_value="key"
        ), patch.object(resume, "install_production_model_contract"), patch.object(
            resume, "install_runtime_closure"
        ), patch.object(resume, "install_gold_vision_capacity_reserve_v1"), patch.object(
            resume, "run_gold_enforce_phase4", side_effect=RuntimeError("gold blocked")
        ):
            with self.assertRaisesRegex(RuntimeError, "gold blocked"):
                resume.execute_gold_resume(**self._execute_kwargs(bundle, durable, accepted, result))
        self.assertEqual(durable.read_bytes(), durable_before)
        self.assertFalse(accepted.exists())
        self.assertEqual(_sha(bundle / "final.mp4"), manifest["final_sha256"])

    def test_already_accepted_durable_output_is_rejected_before_gold(self) -> None:
        _, bundle, durable, accepted, result, manifest = self._fixture()
        durable.write_text(
            json.dumps({"videos": [{"output": OUTPUT_KEY, "release_status": "accepted_after_final_critic"}]}),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"GITHUB_SHA": RUNTIME_RUNNER_SHA}, clear=False), patch.object(
            resume, "validate_resume_bundle", return_value=manifest
        ), patch.object(
            resume, "_git_head", side_effect=[RUNTIME_RUNNER_SHA, RUNTIME_ENGINE_SHA]
        ), patch.object(resume, "_output_key", return_value=OUTPUT_KEY):
            with self.assertRaisesRegex(RuntimeError, "already accepted"):
                resume.execute_gold_resume(**self._execute_kwargs(bundle, durable, accepted, result))

    def test_runtime_must_be_current_certified_checkout_not_historical_source(self) -> None:
        _, bundle, durable, accepted, result, manifest = self._fixture()
        with patch.dict(os.environ, {"GITHUB_SHA": RUNTIME_RUNNER_SHA}, clear=False), patch.object(
            resume, "validate_resume_bundle", return_value=manifest
        ), patch.object(
            resume, "_git_head", side_effect=[SOURCE_RUNNER_SHA, RUNTIME_ENGINE_SHA]
        ), patch.object(resume, "_output_key", return_value=OUTPUT_KEY):
            with self.assertRaisesRegex(RuntimeError, "certified resume SHA"):
                resume.execute_gold_resume(**self._execute_kwargs(bundle, durable, accepted, result))

    def test_executor_source_contains_no_production_rebuild_entrypoints(self) -> None:
        source = inspect.getsource(resume.execute_gold_resume)
        self.assertNotIn("orchestrator.produce(", source)
        self.assertNotIn("run_final_master_qc(", source)
        self.assertNotIn("build_plan(", source)
        self.assertNotIn("synthesize", source)
        self.assertIn("run_gold_enforce_phase4(", source)


if __name__ == "__main__":
    unittest.main()
