from __future__ import annotations

import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import orchestration_shorts_port as port
from scripts import shorts_production_binding as core
from scripts.orchestration_stage_registry import build_l4_registry


class ShortsStablePortTests(unittest.TestCase):
    def test_prepare_delegates_exactly_once_and_returns_core_evidence_unchanged(self) -> None:
        out = Path("output/run")
        request = {"kind": "short", "request_id": "req-1"}
        evidence = {"stage": "pre_gold"}
        with patch.object(core, "prepare_short_render", return_value=evidence) as prepare:
            result = port.prepare_short_render(out, request)

        self.assertIs(result, evidence)
        prepare.assert_called_once_with(out, request)

    def test_prepare_propagates_core_failure_identity_without_retry_or_translation(self) -> None:
        out = Path("output/run")
        request = {"kind": "short"}
        error = RuntimeError("prepare blocked")
        with patch.object(core, "prepare_short_render", side_effect=error) as prepare:
            with self.assertRaises(RuntimeError) as raised:
                port.prepare_short_render(out, request)

        self.assertIs(raised.exception, error)
        prepare.assert_called_once_with(out, request)

    def test_finalize_delegates_exactly_once_and_returns_core_report_unchanged(self) -> None:
        out = Path("output/run")
        request = {"kind": "short", "request_id": "req-1"}
        pre_gold = {"stage": "pre_gold"}
        report = {"stage": "final", "delivery_allowed": True}
        with patch.object(core, "finalize_short_quality", return_value=report) as finalize:
            result = port.finalize_short_quality(out, request, pre_gold)

        self.assertIs(result, report)
        finalize.assert_called_once_with(out, request, pre_gold)

    def test_finalize_propagates_core_failure_identity_without_retry_or_translation(self) -> None:
        out = Path("output/run")
        request = {"kind": "short"}
        pre_gold = {"stage": "pre_gold"}
        error = RuntimeError("final quality blocked")
        with patch.object(core, "finalize_short_quality", side_effect=error) as finalize:
            with self.assertRaises(RuntimeError) as raised:
                port.finalize_short_quality(out, request, pre_gold)

        self.assertIs(raised.exception, error)
        finalize.assert_called_once_with(out, request, pre_gold)

    def test_stage_registry_binds_shorts_to_exact_port_blob_and_preserves_owners(self) -> None:
        contract = build_l4_registry().get("shorts")
        binding = contract.implementation_binding
        data = Path("scripts/orchestration_shorts_port.py").read_bytes()
        actual_blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()

        self.assertEqual(binding.adapter_id, port.PORT_ID)
        self.assertEqual(binding.source_path, "scripts/orchestration_shorts_port.py")
        self.assertEqual(binding.source_sha, actual_blob)
        self.assertEqual(contract.provider_policy["owner"], port.PROVIDER_OWNER)
        self.assertEqual(contract.retry_policy.owner, port.RETRY_OWNER)
        self.assertFalse(contract.cache_policy.read)
        self.assertFalse(contract.cache_policy.write)
        self.assertEqual(contract.side_effect_policy, "idempotent")

    def test_live_control_caller_uses_only_stable_shorts_seam_and_preserves_two_phase_order(self) -> None:
        source = Path("scripts/run_control_production.py").read_text(encoding="utf-8")
        stable_import = (
            "from scripts.orchestration_shorts_port import "
            "finalize_short_quality, prepare_short_render"
        )
        prepare_call = "short_pre = prepare_short_render(output_dir, runtime_request)"
        gold_call = "result = original_gold(**kwargs)"
        finalize_call = "finalize_short_quality(Path(kwargs[\"output_dir\"]), runtime_request, short_pre)"

        self.assertEqual(source.count(stable_import), 1)
        self.assertNotIn("from scripts.shorts_production_binding import", source)
        self.assertEqual(source.count(prepare_call), 1)
        self.assertEqual(source.count(finalize_call), 1)
        self.assertLess(source.index(prepare_call), source.index(gold_call))
        self.assertLess(source.index(gold_call), source.index(finalize_call))

    def test_certified_shorts_core_remains_byte_identical(self) -> None:
        data = Path("scripts/shorts_production_binding.py").read_bytes()
        actual_blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        self.assertEqual(actual_blob, "48043498da00b320b41f255cde544253db2ccb77")

    def test_port_contains_only_execution_composition_not_shorts_implementation(self) -> None:
        source = Path("scripts/orchestration_shorts_port.py").read_text(encoding="utf-8")
        for forbidden in (
            "json.",
            "shutil.",
            "subprocess.",
            "ffmpeg",
            "ffprobe",
            "evaluate_short_quality_v11",
            "evaluate_channel_identity",
            "validate_promise_payoff",
            ".write_text(",
            "time.sleep",
            "try:",
            "except ",
        ):
            self.assertNotIn(forbidden, source)
        self.assertEqual(source.count("core.prepare_short_render(output_dir, control_request)"), 1)
        self.assertEqual(
            source.count("core.finalize_short_quality(output_dir, control_request, pre_gold)"), 1
        )
        self.assertIn('PROVIDER_OWNER = "canonical-short-child-core"', source)
        self.assertIn('RETRY_OWNER = "canonical-short-child-core"', source)


if __name__ == "__main__":
    unittest.main()
