from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import p0_runtime_master_contract as master
from scripts import runtime_phase


ROOT = Path(__file__).resolve().parents[1]


class P0RuntimeMasterContractTests(unittest.TestCase):
    @staticmethod
    def _write_json(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def _prepared_env(self, root: Path) -> dict[str, str]:
        temp = root / "runner-temp"
        state = temp / "isco-state"
        state.mkdir(parents=True, exist_ok=True)
        history = state / "history.json"
        self._write_json(history, {"videos": []})
        self._write_json(
            state / ".persistent-memory-identity.json",
            {
                "schema_version": 1,
                "save_allowed": True,
                "source": "agent-state",
                "state_commit": "1" * 40,
                "state_sequence": 12,
            },
        )
        self._write_json(
            temp / "preproduction-environment.json",
            {
                "schema_version": 2,
                "ffmpeg_libx264": True,
                "tesseract_arabic": True,
                "ffmpeg_filters": [
                    "blackdetect",
                    "silencedetect",
                    "freezedetect",
                    "loudnorm",
                    "subtitles",
                ],
                "release_namespace": "absent",
            },
        )
        self._write_json(
            temp / "provider-preflight.json",
            {
                "schema_version": 4,
                "overall_status": "pass",
                "hard_failures": [],
                "fallback_degraded": ["groq"],
                "checks": [
                    {"provider": "gemini", "status": "pass"},
                    {"provider": "pexels", "status": "pass"},
                    {"provider": "groq", "status": "block"},
                ],
            },
        )
        self._write_json(
            temp / "planning-envelope-preflight.json",
            {
                "status": "pass",
                "format": "film",
                "required_provider_families": 2,
                "viable_provider_families": ["gemini", "openrouter"],
            },
        )
        snapshot = state / "approved-brief.snapshot.json"
        snapshot.write_text('{"approved_by_user":true}\n', encoding="utf-8")
        snapshot.chmod(0o444)
        snapshot_sha = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        github_env = root / "github-env"
        return {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_WORKFLOW_REF": (
                "mymusa79-tech/Isco-Video-Runner/.github/workflows/"
                "produce-resilient-v4.yml@refs/heads/main"
            ),
            "GITHUB_SHA": "a" * 40,
            "EXPECTED_ENGINE_SHA": "b" * 40,
            "GITHUB_RUN_ID": "12345",
            "GITHUB_RUN_ATTEMPT": "2",
            "RUNNER_TEMP": str(temp),
            "ISCO_HISTORY_PATH": str(history),
            "ISCO_APPROVED_BRIEF_SNAPSHOT_PATH": str(snapshot),
            "ISCO_APPROVED_BRIEF_SNAPSHOT_SHA256": snapshot_sha,
            "GITHUB_ENV": str(github_env),
        }

    def test_master_promotes_only_after_all_preproduction_evidence_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = self._prepared_env(root)
            with patch.dict(os.environ, env, clear=True):
                self.assertFalse(runtime_phase.canonical_runtime_enabled())
                result = master.activate_p0_runtime_master()
                self.assertTrue(runtime_phase.canonical_runtime_enabled())
                self.assertEqual(result["decision"], "pass")
                self.assertEqual(result["runtime_phase"], "canonical_live")
                self.assertEqual(result["runner_sha"], "a" * 40)
                self.assertEqual(result["engine_sha"], "b" * 40)
                exported = Path(env["GITHUB_ENV"]).read_text(encoding="utf-8")
                self.assertIn("ISCO_CANONICAL_RUNTIME=1", exported)
                evidence = json.loads(
                    (Path(env["RUNNER_TEMP"]) / master.EVIDENCE_FILENAME).read_text(encoding="utf-8")
                )
                self.assertEqual(evidence["contract_id"], master.CONTRACT_ID)
                self.assertEqual(evidence["planning_provider_families"], ["gemini", "openrouter"])

    def test_master_fails_closed_before_phase_promotion_on_provider_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = self._prepared_env(root)
            provider = Path(env["RUNNER_TEMP"]) / "provider-preflight.json"
            payload = json.loads(provider.read_text(encoding="utf-8"))
            payload["overall_status"] = "block"
            self._write_json(provider, payload)
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(RuntimeError, "provider readiness did not pass"):
                    master.activate_p0_runtime_master()
                self.assertFalse(runtime_phase.canonical_runtime_enabled())
                self.assertFalse(Path(env["GITHUB_ENV"]).exists())

    def test_master_rejects_writable_or_tampered_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = self._prepared_env(root)
            snapshot = Path(env["ISCO_APPROVED_BRIEF_SNAPSHOT_PATH"])
            snapshot.chmod(0o644)
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(RuntimeError, "missing or writable"):
                    master.activate_p0_runtime_master()
                self.assertFalse(runtime_phase.canonical_runtime_enabled())

    def test_preproduction_bootstrap_does_not_export_live_runtime(self) -> None:
        source = (ROOT / "scripts/persistent_memory.py").read_text(encoding="utf-8")
        self.assertIn("activate_canonical_runtime(persist_workflow_env=False)", source)
        self.assertNotIn("activate_canonical_runtime()\n", source)

    def test_final_preflight_is_the_only_cross_step_runtime_promotion_owner(self) -> None:
        planning = (ROOT / "scripts/planning_envelope_preflight.py").read_text(encoding="utf-8")
        phase = (ROOT / "scripts/runtime_phase.py").read_text(encoding="utf-8")
        self.assertIn("activate_p0_runtime_master()", planning)
        self.assertIn("activate_canonical_runtime(persist_workflow_env=True)", (ROOT / "scripts/p0_runtime_master_contract.py").read_text(encoding="utf-8"))
        self.assertNotIn("canonical_workflow_identity() and explicit", planning)
        self.assertIn("canonical_workflow_identity() and explicit in _TRUE_VALUES", phase)

    def test_production_workflow_orders_all_p0_gates_before_produce(self) -> None:
        workflow = (ROOT / ".github/workflows/produce-resilient-v4.yml").read_text(encoding="utf-8")
        restore = workflow.index("- name: Restore encrypted cross-run memory")
        environment = workflow.index("- name: Verify production environment and release namespace")
        providers = workflow.index("- name: Verify complete provider readiness")
        planning = workflow.index("- name: Certify provider-portable planning envelope")
        produce = workflow.index("- name: Produce with canonical V4 runtime")
        self.assertLess(restore, environment)
        self.assertLess(environment, providers)
        self.assertLess(providers, planning)
        self.assertLess(planning, produce)


if __name__ == "__main__":
    unittest.main()