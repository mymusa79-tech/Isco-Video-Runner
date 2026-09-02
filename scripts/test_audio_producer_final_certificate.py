from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts import audio_producer_final_certificate as certificate
from scripts.audio_producer_repair_lifecycle import REPORT_FILENAME


class AudioProducerFinalCertificateTests(unittest.TestCase):
    def _write(self, root: Path, name: str, value: object) -> None:
        (root / name).write_text(json.dumps(value), encoding="utf-8")

    def _base(self, root: Path, *, fmt: str = "film", audio_streams: int = 1) -> None:
        self._write(root, "plan.json", {"format": fmt})
        self._write(root, "quality-final.json", {"format": fmt, "audio_streams": audio_streams})

    def test_long_requires_core_mux_pass_or_repaired_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._base(root)
            self._write(root, REPORT_FILENAME, {"receipts": [{"phase": "core_mux", "decision": "repaired_pass", "repair_attempts": 1}]})
            receipt = certificate.require_audio_producer_certificate(root)
            self.assertEqual(receipt["decision"], "repaired_pass")

    def test_finished_short_requires_short_finished_receipt_not_stale_core_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._base(root, fmt="moment", audio_streams=1)
            (root / "short-intelligence-pre-gold.json").write_text("{}", encoding="utf-8")
            self._write(root, REPORT_FILENAME, {"receipts": [{"phase": "core_mux", "decision": "pass", "repair_attempts": 0}]})
            with self.assertRaisesRegex(certificate.AudioProducerCertificateError, "missing_phase:short_finished"):
                certificate.require_audio_producer_certificate(root)

    def test_unfinished_silent_moment_may_be_not_applicable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._base(root, fmt="moment", audio_streams=0)
            self._write(root, REPORT_FILENAME, {"receipts": [{"phase": "core_mux", "decision": "not_applicable", "repair_attempts": 0}]})
            receipt = certificate.require_audio_producer_certificate(root)
            self.assertEqual(receipt["decision"], "not_applicable")

    def test_block_or_more_than_one_attempt_never_reaches_final_qc(self) -> None:
        cases = [
            {"phase": "core_mux", "decision": "block", "repair_attempts": 0},
            {"phase": "core_mux", "decision": "repaired_pass", "repair_attempts": 2},
        ]
        for payload in cases:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                self._base(root)
                self._write(root, REPORT_FILENAME, {"receipts": [payload]})
                with self.assertRaises(certificate.AudioProducerCertificateError):
                    certificate.require_audio_producer_certificate(root)

    def test_wrapper_certifies_before_existing_final_qc_chain(self) -> None:
        events: list[str] = []
        production = SimpleNamespace(run_final_master_qc=lambda out: events.append("existing") or {"decision": "pass"})
        original = production.run_final_master_qc
        try:
            old_require = certificate.require_audio_producer_certificate
            certificate.require_audio_producer_certificate = lambda out: events.append("audio-producer") or {}
            certificate.install_audio_producer_final_certificate([production])
            production.run_final_master_qc(Path("output/x"))
            self.assertEqual(events, ["audio-producer", "existing"])
        finally:
            certificate.require_audio_producer_certificate = old_require
            production.run_final_master_qc = original


if __name__ == "__main__":
    unittest.main()
