from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts import audio_producer_final_certificate as certificate
from scripts.audio_producer_repair_lifecycle import REPORT_FILENAME, SCHEMA_VERSION


class AudioProducerFinalCertificateTests(unittest.TestCase):
    def _write(self, root: Path, name: str, value: object) -> None:
        (root / name).write_text(json.dumps(value), encoding="utf-8")

    def _base(self, root: Path, *, fmt: str = "film", audio_streams: int = 1) -> Path:
        final = root / "final.mp4"
        final.write_bytes(b"final-audio-producer-fixture" * 128)
        self._write(root, "plan.json", {"format": fmt})
        self._write(root, "quality-final.json", {"format": fmt, "audio_streams": audio_streams})
        return final

    def _receipt(self, final: Path, *, phase: str, decision: str, attempts: int) -> dict:
        return {
            "phase": phase,
            "decision": decision,
            "repair_attempts": attempts,
            "final_sha256": certificate._sha256_file(final),
        }

    def _report(self, root: Path, receipts: list[dict]) -> None:
        self._write(root, REPORT_FILENAME, {"schema_version": SCHEMA_VERSION, "receipts": receipts})

    def test_long_requires_core_mux_pass_or_repaired_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            final = self._base(root)
            self._report(root, [self._receipt(final, phase="core_mux", decision="repaired_pass", attempts=1)])
            receipt = certificate.require_audio_producer_certificate(root)
            self.assertEqual(receipt["decision"], "repaired_pass")

    def test_finished_short_requires_short_finished_receipt_not_stale_core_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            final = self._base(root, fmt="moment", audio_streams=1)
            (root / "short-intelligence-pre-gold.json").write_text("{}", encoding="utf-8")
            self._report(root, [self._receipt(final, phase="core_mux", decision="pass", attempts=0)])
            with self.assertRaisesRegex(certificate.AudioProducerCertificateError, "missing_phase:short_finished"):
                certificate.require_audio_producer_certificate(root)

    def test_unfinished_silent_moment_may_be_not_applicable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            final = self._base(root, fmt="moment", audio_streams=0)
            self._report(root, [self._receipt(final, phase="core_mux", decision="not_applicable", attempts=0)])
            receipt = certificate.require_audio_producer_certificate(root)
            self.assertEqual(receipt["decision"], "not_applicable")

    def test_stale_pass_receipt_is_rejected_after_final_byte_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            final = self._base(root)
            receipt = self._receipt(final, phase="core_mux", decision="pass", attempts=0)
            self._report(root, [receipt])
            with final.open("ab") as handle:
                handle.write(b"mutated-after-audio-producer-pre-gate")
            with self.assertRaisesRegex(
                certificate.AudioProducerCertificateError,
                "final_identity_mismatch",
            ):
                certificate.require_audio_producer_certificate(root)

    def test_block_or_more_than_one_attempt_never_reaches_final_qc(self) -> None:
        cases = [
            ("block", 0),
            ("repaired_pass", 2),
        ]
        for decision, attempts in cases:
            with self.subTest(decision=decision, attempts=attempts), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                final = self._base(root)
                self._report(root, [self._receipt(final, phase="core_mux", decision=decision, attempts=attempts)])
                with self.assertRaises(certificate.AudioProducerCertificateError):
                    certificate.require_audio_producer_certificate(root)

    def test_schema_mismatch_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            final = self._base(root)
            self._write(root, REPORT_FILENAME, {
                "schema_version": SCHEMA_VERSION + 1,
                "receipts": [self._receipt(final, phase="core_mux", decision="pass", attempts=0)],
            })
            with self.assertRaisesRegex(certificate.AudioProducerCertificateError, "schema_mismatch"):
                certificate.require_audio_producer_certificate(root)

    def test_wrapper_certifies_before_existing_final_qc_chain_and_is_idempotent(self) -> None:
        events: list[str] = []
        production = SimpleNamespace(run_final_master_qc=lambda out: events.append("existing") or {"decision": "pass"})
        original = production.run_final_master_qc
        old_require = certificate.require_audio_producer_certificate
        try:
            certificate.require_audio_producer_certificate = lambda out: events.append("audio-producer") or {}
            certificate.install_audio_producer_final_certificate([production])
            first = production.run_final_master_qc
            certificate.install_audio_producer_final_certificate([production])
            self.assertIs(production.run_final_master_qc, first)
            production.run_final_master_qc(Path("output/x"))
            self.assertEqual(events, ["audio-producer", "existing"])
        finally:
            certificate.require_audio_producer_certificate = old_require
            production.run_final_master_qc = original


if __name__ == "__main__":
    unittest.main()
