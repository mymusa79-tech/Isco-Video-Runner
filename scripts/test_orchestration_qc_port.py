from __future__ import annotations

import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import final_master_qc as core
from scripts import orchestration_qc_port as port
from scripts.orchestration_stage_registry import build_l4_registry


class QCStablePortTests(unittest.TestCase):
    def test_port_delegates_exactly_once_and_returns_core_report_unchanged(self) -> None:
        out = Path("output/run")
        report = {"status": "pass", "production_stage": "post_render_pre_gold_acceptance"}
        with patch.object(core, "run_final_master_qc", return_value=report) as qc:
            result = port.run_final_master_qc(out)

        self.assertIs(result, report)
        qc.assert_called_once_with(out)

    def test_port_propagates_authoritative_core_failure_without_retry_or_translation(self) -> None:
        out = Path("output/run")
        error = core.FinalMasterQCError("blocked")
        with patch.object(core, "run_final_master_qc", side_effect=error) as qc:
            with self.assertRaises(core.FinalMasterQCError) as raised:
                port.run_final_master_qc(out)

        self.assertIs(raised.exception, error)
        qc.assert_called_once_with(out)

    def test_stage_registry_binds_qc_to_exact_port_blob_and_preserves_owners(self) -> None:
        contract = build_l4_registry().get("qc")
        binding = contract.implementation_binding
        data = Path("scripts/orchestration_qc_port.py").read_bytes()
        actual_blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()

        self.assertEqual(binding.adapter_id, port.PORT_ID)
        self.assertEqual(binding.source_path, "scripts/orchestration_qc_port.py")
        self.assertEqual(binding.source_sha, actual_blob)
        self.assertEqual(contract.provider_policy["owner"], port.PROVIDER_OWNER)
        self.assertEqual(contract.retry_policy.owner, port.RETRY_OWNER)
        self.assertFalse(contract.cache_policy.read)
        self.assertFalse(contract.cache_policy.write)
        self.assertEqual(contract.side_effect_policy, "none")

    def test_entrypoint_uses_stable_qc_seam_once_in_exact_pre_gold_position(self) -> None:
        source = Path("scripts/run_v3_voice.py").read_text(encoding="utf-8")
        stable_import = "from scripts.orchestration_qc_port import run_final_master_qc"
        qc_call = "run_final_master_qc(out)"
        gold_call = "run_gold_enforce_phase4("

        self.assertEqual(source.count(stable_import), 1)
        self.assertNotIn("from scripts.final_master_qc import run_final_master_qc", source)
        self.assertEqual(source.count(qc_call), 1)
        self.assertLess(source.index(qc_call), source.index(gold_call))

    def test_certified_final_master_qc_core_remains_byte_identical(self) -> None:
        data = Path("scripts/final_master_qc.py").read_bytes()
        actual_blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        self.assertEqual(actual_blob, "e3412fc5710618eb9d7529710d8dbbc539e9fa91")

    def test_port_contains_only_execution_composition_not_qc_implementation(self) -> None:
        source = Path("scripts/orchestration_qc_port.py").read_text(encoding="utf-8")
        for forbidden in (
            "subprocess.run",
            "blackdetect",
            "silencedetect",
            "freezedetect",
            "probe(",
            "report_path.write_text",
            "blocking.append",
            "time.sleep",
        ):
            self.assertNotIn(forbidden, source)
        self.assertEqual(source.count("core.run_final_master_qc(output_dir)"), 1)
        self.assertIn('PROVIDER_OWNER = "final-master-qc-core"', source)
        self.assertIn('RETRY_OWNER = "final-master-qc-core"', source)


if __name__ == "__main__":
    unittest.main()
