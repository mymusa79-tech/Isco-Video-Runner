from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import planning_production_contract_v2 as family
from scripts import planning_stage_contract as stage_contract


class PlanningProductionContractV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        family.reset_runtime_evidence_for_tests()
        family._STAGE_RECEIPTS.append(
            {
                "sequence": 1,
                "stage_id": "planning.editorial_outline",
                "contract_id": "planning.editorial_outline.v1",
                "input_hash": "a" * 64,
                "contract_fingerprint": "b" * 64,
                "requested_model": "gemini-3.7-flash",
                "accepted_provider": "gemini",
                "cache_hit": False,
                "cache_revalidated": False,
                "output_sha256": "c" * 64,
                "deadline_policy": family.deadline_policy().as_dict(),
            }
        )
        family._FAMILY_STARTED_AT = 100.0

    def _write_case(self, root: Path, fmt: str) -> None:
        plan = {
            "format": fmt,
            "topic": "كيف تنهض عندما تفقد الدافع تمامًا؟",
            "sections": [{"id": "s1"}],
        }
        (root / "plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (root / "research-provenance.json").write_text(
            json.dumps({"approved_research_pack": []}, ensure_ascii=False),
            encoding="utf-8",
        )
        gate_payloads = {
            "repair-dossier.json": {"repair_status": "not_needed"},
            "factuality-audit.json": {"status": "pass"},
            "content-quality-audit.json": {"status": "pass"},
            "tone-quality-audit.json": {"status": "pass"},
            "quality-precheck.json": {
                "factuality_status": "pass",
                "content_quality_status": "pass",
                "tone_quality_status": "pass",
            },
        }
        for filename, payload in gate_payloads.items():
            (root / filename).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def _identity_patches(self):
        return (
            patch.object(
                family,
                "_approved_input_identity",
                return_value={"path": "approved-brief.snapshot.json", "sha256": "d" * 64},
            ),
            patch.object(
                family,
                "_research_identity",
                return_value={"file": "research-provenance.json", "sha256": "e" * 64},
            ),
            patch.object(family, "_runtime_contract_sha256", return_value="f" * 64),
        )

    def test_deadline_policy_is_part_of_stage_contract_fingerprint(self) -> None:
        original_bind = stage_contract.bind_request_contract
        original_read = stage_contract._cache_read
        original_commit = stage_contract._cache_commit
        try:
            spec = stage_contract.outline_stage_spec(3)
            before = original_bind(spec, "same prompt")
            family._install_stage_evidence_hooks()
            after = stage_contract.bind_request_contract(spec, "same prompt")
            self.assertIn("deadline_policy", after.semantic_rules)
            self.assertNotEqual(
                stage_contract._contract_fingerprint(before),
                stage_contract._contract_fingerprint(after),
            )
            self.assertNotEqual(
                stage_contract._cache_key(before, "gemini-3.7-flash"),
                stage_contract._cache_key(after, "gemini-3.7-flash"),
            )
        finally:
            stage_contract.bind_request_contract = original_bind
            stage_contract._cache_read = original_read
            stage_contract._cache_commit = original_commit

    def test_auth_and_deterministic_config_failure_get_explicit_family_taxonomy(self) -> None:
        original = stage_contract._provider_failure
        try:
            family._install_error_taxonomy_hook()
            contract = stage_contract.bind_request_contract(
                stage_contract.outline_stage_spec(3), "prompt"
            )
            error, retryable, _provider_delay, failure = stage_contract._provider_failure(
                contract,
                "gemini",
                RuntimeError("HTTP 401 Unauthorized invalid API key"),
            )
            self.assertEqual(error.code.value, "AUTH_CONFIG")
            self.assertFalse(retryable)
            self.assertEqual(failure.telemetry_result, "auth_error")
        finally:
            stage_contract._provider_failure = original

    def test_long_final_plan_gets_exact_family_certificate_before_p2(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_case(root, "film")
            approved, research, runtime = self._identity_patches()
            with approved, research, runtime, patch.object(
                family.time, "monotonic", return_value=111.0
            ):
                report = family.certify_planning_handoff(root)
                self.assertEqual(report["decision"], "pass")
                self.assertEqual(report["format"], "film")
                self.assertEqual(report["stage_receipt_count"], 1)
                self.assertEqual(set(report["planning_gate_evidence"]), set(family._PLANNING_GATE_ARTIFACTS))
                self.assertEqual(
                    family.require_planning_handoff(root)["contract_id"],
                    family.CONTRACT_ID,
                )

    def test_short_uses_same_family_contract(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_case(root, "moment")
            approved, research, runtime = self._identity_patches()
            with approved, research, runtime, patch.object(
                family.time, "monotonic", return_value=105.0
            ):
                report = family.certify_planning_handoff(root)
            self.assertEqual(report["family_id"], family.FAMILY_ID)
            self.assertEqual(report["contract_id"], family.CONTRACT_ID)
            self.assertEqual(report["format"], "moment")

    def test_plan_tamper_after_certificate_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_case(root, "film")
            approved, research, runtime = self._identity_patches()
            with approved, research, runtime, patch.object(
                family.time, "monotonic", return_value=101.0
            ):
                family.certify_planning_handoff(root)
                plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
                plan["topic"] = "موضوع مختلف"
                (root / "plan.json").write_text(
                    json.dumps(plan, ensure_ascii=False), encoding="utf-8"
                )
                with self.assertRaises(stage_contract.PlanningStageError) as captured:
                    family.require_planning_handoff(root)
            self.assertEqual(captured.exception.code.value, "FINAL_PLAN_INVALID")

    def test_planning_gate_evidence_tamper_after_certificate_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_case(root, "film")
            approved, research, runtime = self._identity_patches()
            with approved, research, runtime, patch.object(
                family.time, "monotonic", return_value=101.0
            ):
                family.certify_planning_handoff(root)
                (root / "tone-quality-audit.json").write_text(
                    json.dumps({"status": "changed"}), encoding="utf-8"
                )
                with self.assertRaises(stage_contract.PlanningStageError) as captured:
                    family.require_planning_handoff(root)
            self.assertEqual(captured.exception.code.value, "LINEAGE_INVALID")

    def test_approved_input_tamper_invalidates_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_case(root, "film")
            with patch.object(
                family,
                "_approved_input_identity",
                return_value={"path": "brief.json", "sha256": "1" * 64},
            ), patch.object(
                family,
                "_research_identity",
                return_value={"file": "research-provenance.json", "sha256": "2" * 64},
            ), patch.object(
                family, "_runtime_contract_sha256", return_value="3" * 64
            ), patch.object(family.time, "monotonic", return_value=101.0):
                family.certify_planning_handoff(root)
            with patch.object(
                family,
                "_approved_input_identity",
                return_value={"path": "brief.json", "sha256": "4" * 64},
            ), patch.object(
                family,
                "_research_identity",
                return_value={"file": "research-provenance.json", "sha256": "2" * 64},
            ), patch.object(family, "_runtime_contract_sha256", return_value="3" * 64):
                with self.assertRaises(stage_contract.PlanningStageError) as captured:
                    family.require_planning_handoff(root)
            self.assertEqual(captured.exception.code.value, "LINEAGE_INVALID")

    def test_family_wall_deadline_is_format_specific_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_case(root, "moment")
            approved, research, runtime = self._identity_patches()
            with approved, research, runtime, patch.object(
                family.time,
                "monotonic",
                return_value=100.0 + family.deadline_policy().short_family_wall_seconds + 0.01,
            ):
                with self.assertRaises(stage_contract.PlanningStageError) as captured:
                    family.certify_planning_handoff(root)
            self.assertEqual(captured.exception.code.value, "DEADLINE_EXCEEDED")

    def test_plan_source_annotation_rebinds_bytes_without_changing_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_case(root, "film")
            approved, research, runtime = self._identity_patches()
            with approved, research, runtime, patch.object(
                family.time, "monotonic", return_value=102.0
            ):
                before = family.certify_planning_handoff(root)
                family._rebind_plan_source_annotation(root, "gemini+groq")
                after = family.require_planning_handoff(root)
            self.assertEqual(
                before["final_plan_semantic_sha256"],
                after["final_plan_semantic_sha256"],
            )
            self.assertNotEqual(
                before["final_plan_file_sha256"],
                after["final_plan_file_sha256"],
            )
            self.assertEqual(after["annotations"]["plan_source"], "gemini+groq")

    def test_handoff_gate_certifies_before_director_observer_runs(self) -> None:
        calls: list[str] = []
        original = family.orchestrator._observe_director_phase_a
        family.orchestrator._observe_director_phase_a = lambda *a, **k: calls.append("director") or "ok"
        try:
            family._install_handoff_gate()
            with patch.object(
                family,
                "certify_planning_handoff",
                side_effect=lambda *_a, **_k: calls.append("certify") or {},
            ), patch.object(
                family,
                "require_planning_handoff",
                side_effect=lambda *_a, **_k: calls.append("require") or {},
            ):
                result = family.orchestrator._observe_director_phase_a(
                    out=Path("/tmp/f23"), plan=SimpleNamespace(format="film")
                )
            self.assertEqual(result, "ok")
            self.assertEqual(calls, ["certify", "require", "director"])
        finally:
            family.orchestrator._observe_director_phase_a = original


if __name__ == "__main__":
    unittest.main()
