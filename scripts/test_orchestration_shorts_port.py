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

    def test_authoritative_pre_gold_seam_orders_prepare_voice_then_re_qc(self) -> None:
        out = Path("output/run")
        request = {"kind": "short", "request_id": "req-1"}
        ledger = object()
        prepared = {"stage": "pre_gold"}
        voiced = {"stage": "pre_gold", "voice": {"generated": True}}
        order: list[str] = []

        def prepare(_out, _request):
            order.append("prepare")
            return prepared

        def voice(_out, _request, pre_gold, *, ledger):
            self.assertIs(pre_gold, prepared)
            self.assertIsNotNone(ledger)
            order.append("voice")
            return voiced

        def qc(_out):
            order.append("qc")
            return {"status": "pass", "final_media_mutated": False}

        with patch.object(core, "prepare_short_render", side_effect=prepare) as prepare_mock, patch.object(
            port, "apply_short_voice_v2", side_effect=voice
        ) as voice_mock:
            result = port.prepare_authoritative_short_for_gold(
                out,
                request,
                ledger=ledger,
                run_final_master_qc=qc,
            )

        self.assertIs(result, voiced)
        self.assertTrue(result["authoritative_final_master_qc_rerun"])
        self.assertEqual(order, ["prepare", "voice", "qc"])
        prepare_mock.assert_called_once()
        voice_mock.assert_called_once()

    def test_authoritative_pre_gold_seam_blocks_failed_re_qc(self) -> None:
        out = Path("output/run")
        request = {"kind": "short", "request_id": "req-1"}
        with patch.object(core, "prepare_short_render", return_value={"stage": "pre_gold"}), patch.object(
            port, "apply_short_voice_v2", return_value={"stage": "pre_gold"}
        ):
            with self.assertRaisesRegex(RuntimeError, "authoritative Final Master QC did not pass"):
                port.prepare_authoritative_short_for_gold(
                    out,
                    request,
                    ledger=object(),
                    run_final_master_qc=lambda _out: {"status": "block", "final_media_mutated": False},
                )

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

    def test_live_short_callers_use_shared_authoritative_pre_gold_seam(self) -> None:
        control = Path("scripts/run_control_production.py").read_text(encoding="utf-8")
        canonical = Path("scripts/canonical_v4_short_child.py").read_text(encoding="utf-8")
        seam = "prepare_authoritative_short_for_gold("

        for source in (control, canonical):
            self.assertEqual(source.count(seam), 1)
            self.assertNotIn("from scripts.shorts_production_binding import", source)
            self.assertNotIn("from scripts.short_voice_v2 import apply_short_voice_v2", source)
            self.assertIn("run_final_master_qc=production.run_final_master_qc", source)
            seam_index = source.index(seam)
            gold_index = source.index("result = original_gold(**kwargs)")
            finalize_index = source.index("finalize_short_quality(")
            self.assertLess(seam_index, gold_index)
            self.assertLess(gold_index, finalize_index)

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
        self.assertEqual(source.count("core.prepare_short_render(output_dir, control_request)"), 2)
        self.assertEqual(
            source.count("core.finalize_short_quality(output_dir, control_request, pre_gold)"), 1
        )
        self.assertIn("apply_short_voice_v2(", source)
        self.assertIn("run_final_master_qc(output_dir)", source)
        self.assertIn('PROVIDER_OWNER = "canonical-short-child-core"', source)
        self.assertIn('RETRY_OWNER = "canonical-short-child-core"', source)


if __name__ == "__main__":
    unittest.main()
