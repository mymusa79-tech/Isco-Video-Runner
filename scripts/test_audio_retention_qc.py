from __future__ import annotations

import json
import math
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from scripts import audio_retention_qc as qc


class AudioRetentionQCTests(unittest.TestCase):
    def _tone(self, seconds: float, *, amplitude: int = 6000, rate: int = qc.ANALYSIS_SAMPLE_RATE) -> list[int]:
        count = int(round(seconds * rate))
        return [int(amplitude * math.sin(2.0 * math.pi * 440.0 * i / rate)) for i in range(count)]

    def _write_json(self, root: Path, name: str, value: object) -> None:
        (root / name).write_text(json.dumps(value), encoding="utf-8")

    def _fake_decoder(self, samples: list[int], *, rate: int = qc.ANALYSIS_SAMPLE_RATE):
        def decode(_final: Path, wav_path: Path) -> None:
            with wave.open(str(wav_path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(rate)
                payload = array("h", samples)
                handle.writeframes(payload.tobytes())
        return decode

    def test_short_blocks_retention_killing_opening_silence(self) -> None:
        samples = [0] * int(0.60 * qc.ANALYSIS_SAMPLE_RATE) + self._tone(1.5)
        report = qc.analyze_pcm(samples, qc.ANALYSIS_SAMPLE_RATE, qc.SHORT_PROFILE)
        self.assertTrue(any(item.startswith("opening_audio_dropout=") for item in report["blocking_findings"]))

    def test_long_preserves_existing_opening_tolerance(self) -> None:
        samples = [0] * int(0.60 * qc.ANALYSIS_SAMPLE_RATE) + self._tone(2.0)
        report = qc.analyze_pcm(samples, qc.ANALYSIS_SAMPLE_RATE, qc.LONG_PROFILE)
        self.assertFalse(any(item.startswith("opening_audio_dropout=") for item in report["blocking_findings"]))

    def test_short_blocks_interior_dropout_before_legacy_four_second_gate(self) -> None:
        samples = self._tone(0.8) + [0] * int(1.40 * qc.ANALYSIS_SAMPLE_RATE) + self._tone(0.8)
        report = qc.analyze_pcm(samples, qc.ANALYSIS_SAMPLE_RATE, qc.SHORT_PROFILE)
        self.assertTrue(any(item.startswith("interior_audio_dropout=") for item in report["blocking_findings"]))

    def test_sustained_clipping_is_fail_closed(self) -> None:
        samples = self._tone(0.8) + [32767] * 240 + self._tone(0.8)
        report = qc.analyze_pcm(samples, qc.ANALYSIS_SAMPLE_RATE, qc.SHORT_PROFILE)
        self.assertTrue(any(item.startswith("sustained_clipping") for item in report["blocking_findings"]))

    def test_gain_jump_is_warning_not_new_repair_authority(self) -> None:
        samples = self._tone(0.5, amplitude=1200) + self._tone(0.5, amplitude=14000)
        report = qc.analyze_pcm(samples, qc.ANALYSIS_SAMPLE_RATE, qc.SHORT_PROFILE)
        self.assertFalse(report["blocking_findings"])
        self.assertTrue(any(item.startswith("gain_jump_suspected_count=") for item in report["warnings"]))

    def test_source_derived_short_profile_and_exact_byte_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            final = root / "final.mp4"
            final.write_bytes(b"retention-final-fixture" * 128)
            self._write_json(root, "plan.json", {"format": "moment"})
            self._write_json(root, "quality-final.json", {"format": "moment", "audio_streams": 1})
            self._write_json(
                root,
                "short-intelligence-pre-gold.json",
                {"compensation": {"scope": "short_sibling"}},
            )
            samples = self._tone(2.0)
            report = qc.run_audio_retention_qc(root, decoder=self._fake_decoder(samples))
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["scope"], "source_derived_short")
            self.assertEqual(report["profile"]["name"], "short_retention_v1")
            self.assertEqual(report["final"]["sha256"], qc._sha256_file(final))
            self.assertEqual(report["policy"]["ai_calls_added"], 0)
            self.assertTrue((root / qc.REPORT_FILENAME).is_file())


if __name__ == "__main__":
    unittest.main()
