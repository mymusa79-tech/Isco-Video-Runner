from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import audio_production_contract_v2 as contract


class AudioProductionContractV2Tests(unittest.TestCase):
    def _write_json(self, root: Path, name: str, value: object) -> None:
        (root / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def _base_long(self, root: Path, transcript: str = "لا تستسلم عندما يصبح الطريق صعبا") -> None:
        (root / "final.mp4").write_bytes(b"final-audio-v2" * 256)
        self._write_json(
            root,
            "plan.json",
            {"format": "film", "sections": [{"narration": transcript}]},
        )

    def _base_short(self, root: Path, transcript: str = "ابدأ بخطوة صغيرة ثم استمر") -> None:
        (root / "final.mp4").write_bytes(b"final-short-audio-v2" * 256)
        self._write_json(root, "plan.json", {"format": "moment"})
        self._write_json(
            root,
            "short-intelligence-pre-gold.json",
            {"voice": {"transcript": transcript}},
        )

    @staticmethod
    def _extract(_final: Path, audio: Path) -> None:
        audio.write_bytes(b"flac-audio-evidence" * 64)

    def test_long_primary_pass_uses_one_provider_attempt(self) -> None:
        expected = "لا تستسلم عندما يصبح الطريق صعبا"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._base_long(root, expected)
            result = contract.require_audio_production_contract_v2(
                root,
                extractor=self._extract,
                groq_transcriber=lambda _audio: expected,
                gemini_transcriber=lambda _audio: self.fail("fallback must not run"),
            )
            self.assertEqual(result["decision"], "pass")
            self.assertEqual(result["scope"], "long")
            self.assertEqual(result["accepted_provider"], "groq-whisper")
            self.assertFalse(result["fallback_used"])
            self.assertEqual(len(result["attempts"]), 1)

    def test_short_uses_finished_voice_transcript_not_plan_narration(self) -> None:
        expected = "ابدأ بخطوة صغيرة ثم استمر"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._base_short(root, expected)
            result = contract.require_audio_production_contract_v2(
                root,
                extractor=self._extract,
                groq_transcriber=lambda _audio: expected,
                gemini_transcriber=lambda _audio: self.fail("fallback must not run"),
            )
            self.assertEqual(result["decision"], "pass")
            self.assertEqual(result["scope"], "short")
            self.assertEqual(result["expected_source"], "short-intelligence-pre-gold.json")

    def test_primary_technical_failure_falls_back_once(self) -> None:
        expected = "لا تؤجل الخطوة التي تستطيع فعلها اليوم"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._base_long(root, expected)
            result = contract.require_audio_production_contract_v2(
                root,
                extractor=self._extract,
                groq_transcriber=lambda _audio: (_ for _ in ()).throw(RuntimeError("429 quota exhausted")),
                gemini_transcriber=lambda _audio: expected,
            )
            self.assertEqual(result["decision"], "pass")
            self.assertTrue(result["fallback_used"])
            self.assertEqual(result["accepted_provider"], "gemini-audio")
            self.assertEqual(len(result["attempts"]), 2)
            self.assertEqual(result["attempts"][0]["error_code"], "PROVIDER_CAPACITY")

    def test_primary_semantic_review_requires_independent_confirmation(self) -> None:
        expected = "لا تفعل هذا الان"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._base_long(root, expected)
            result = contract.require_audio_production_contract_v2(
                root,
                extractor=self._extract,
                groq_transcriber=lambda _audio: "افعل هذا الان",
                gemini_transcriber=lambda _audio: expected,
            )
            self.assertEqual(result["decision"], "pass")
            self.assertEqual(result["attempts"][0]["status"], "semantic_review")
            self.assertEqual(result["attempts"][1]["status"], "pass")

    def test_two_semantic_mismatches_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._base_long(root, "لا تتراجع عن قرارك")
            with self.assertRaisesRegex(contract.AudioProductionContractError, "SEMANTIC_MISMATCH"):
                contract.require_audio_production_contract_v2(
                    root,
                    extractor=self._extract,
                    groq_transcriber=lambda _audio: "تراجع عن قرارك",
                    gemini_transcriber=lambda _audio: "غير قرارك",
                )
            audit = json.loads((root / contract.AUDIT_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(audit["decision"], "block")
            self.assertEqual(audit["error_code"], "SEMANTIC_MISMATCH")
            self.assertEqual(len(audit["attempts"]), 2)

    def test_two_technical_failures_are_audit_unavailable_not_semantic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._base_long(root)
            with self.assertRaisesRegex(contract.AudioProductionContractError, "AUDIT_UNAVAILABLE"):
                contract.require_audio_production_contract_v2(
                    root,
                    extractor=self._extract,
                    groq_transcriber=lambda _audio: (_ for _ in ()).throw(TimeoutError("timed out")),
                    gemini_transcriber=lambda _audio: (_ for _ in ()).throw(RuntimeError("503 unavailable")),
                )
            audit = json.loads((root / contract.AUDIT_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(audit["attempts"][0]["error_code"], "PROVIDER_TRANSIENT")
            self.assertEqual(audit["attempts"][1]["error_code"], "PROVIDER_TRANSIENT")

    def test_silent_unfinished_moment_is_not_applicable_without_provider_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "final.mp4").write_bytes(b"silent-moment" * 256)
            self._write_json(root, "plan.json", {"format": "moment"})
            result = contract.require_audio_production_contract_v2(
                root,
                extractor=lambda *_args: self.fail("extractor must not run"),
                groq_transcriber=lambda _audio: self.fail("provider must not run"),
                gemini_transcriber=lambda _audio: self.fail("provider must not run"),
            )
            self.assertEqual(result["decision"], "not_applicable")
            self.assertEqual(result["attempts"], [])

    def test_provider_budget_is_hard_capped_at_two(self) -> None:
        self.assertEqual(contract.MAX_PROVIDER_ATTEMPTS, 2)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._base_long(root)
            calls: list[str] = []

            def fail(name: str):
                def inner(_audio: Path) -> str:
                    calls.append(name)
                    raise RuntimeError("network unavailable")
                return inner

            with self.assertRaises(contract.AudioProductionContractError):
                contract.require_audio_production_contract_v2(
                    root,
                    extractor=self._extract,
                    groq_transcriber=fail("groq"),
                    gemini_transcriber=fail("gemini"),
                )
            self.assertEqual(calls, ["groq", "gemini"])


if __name__ == "__main__":
    unittest.main()
