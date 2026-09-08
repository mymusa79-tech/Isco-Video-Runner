from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import audio_production_contract_v2 as contract
from scripts.audio_production_resume_v1 import (
    require_existing_audio_production_pass,
    resume_audio_production_contract_v2,
)


class AudioProductionResumeV1Tests(unittest.TestCase):
    def _root(self) -> tuple[tempfile.TemporaryDirectory[str], Path, str]:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        transcript = "هذا نص عربي واضح لاختبار سلامة الكلام النهائي"
        (root / "final.mp4").write_bytes(b"video-bytes" * 300)
        (root / "plan.json").write_text(
            json.dumps({"format": "moment", "sections": [{"id": "s1"}]}),
            encoding="utf-8",
        )
        (root / "short-intelligence-pre-gold.json").write_text(
            json.dumps({"voice": {"transcript": transcript}}),
            encoding="utf-8",
        )
        return temp, root, transcript

    @staticmethod
    def _extractor(_final: Path, audio: Path) -> None:
        audio.write_bytes(b"flac" * 500)

    def _write_prior(self, root: Path, transcript: str, attempts: list[dict]) -> None:
        final_sha = contract._sha256_file(root / "final.mp4")
        doc = {
            "schema_version": contract.SCHEMA_VERSION,
            "contract_id": contract.CONTRACT_ID,
            "decision": "block",
            "error_code": contract.AudioContractErrorCode.AUDIT_UNAVAILABLE.value,
            "final_sha256": final_sha,
            "scope": "short",
            "expected_source": "short-intelligence-pre-gold.json",
            "expected_transcript_sha256": contract._sha256_text(transcript),
            "attempts": attempts,
        }
        (root / contract.AUDIT_FILENAME).write_text(json.dumps(doc), encoding="utf-8")

    def test_semantic_plus_technical_retries_only_failed_independent_provider(self) -> None:
        temp, root, transcript = self._root()
        self.addCleanup(temp.cleanup)
        self._write_prior(
            root,
            transcript,
            [
                {"provider": "groq-whisper", "status": "semantic_review", "transcript_sha256": "a" * 64},
                {"provider": "gemini-audio", "status": "technical_failure", "error_code": "PROVIDER_CAPACITY"},
            ],
        )

        def groq_forbidden(_audio: Path) -> str:
            raise AssertionError("existing semantic reviewer must not be re-rolled")

        result = resume_audio_production_contract_v2(
            root,
            extractor=self._extractor,
            groq_transcriber=groq_forbidden,
            gemini_transcriber=lambda _audio: transcript,
        )
        self.assertEqual(result["decision"], "pass")
        self.assertEqual(result["accepted_provider"], "gemini-audio")
        self.assertEqual(result["resume_provider_attempts"], 1)
        self.assertTrue(result["resume_policy"]["approval_shopping_forbidden"])
        require_existing_audio_production_pass(root)

    def test_second_independent_semantic_review_is_terminal_mismatch(self) -> None:
        temp, root, transcript = self._root()
        self.addCleanup(temp.cleanup)
        self._write_prior(
            root,
            transcript,
            [
                {"provider": "groq-whisper", "status": "semantic_review", "transcript_sha256": "a" * 64},
                {"provider": "gemini-audio", "status": "technical_failure", "error_code": "PROVIDER_TRANSIENT"},
            ],
        )
        with self.assertRaises(contract.AudioProductionContractError) as caught:
            resume_audio_production_contract_v2(
                root,
                extractor=self._extractor,
                groq_transcriber=lambda _audio: (_ for _ in ()).throw(AssertionError("groq must not rerun")),
                gemini_transcriber=lambda _audio: "كلمات أخرى مختلفة بالكامل ولا تطابق النص المتوقع إطلاقا",
            )
        self.assertIs(caught.exception.code, contract.AudioContractErrorCode.SEMANTIC_MISMATCH)
        saved = json.loads((root / contract.AUDIT_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(saved["error_code"], "SEMANTIC_MISMATCH")
        self.assertEqual(saved["resume_provider_attempts"], 1)

    def test_failed_independent_provider_remains_unavailable_without_rerolling_semantic_review(self) -> None:
        temp, root, transcript = self._root()
        self.addCleanup(temp.cleanup)
        self._write_prior(
            root,
            transcript,
            [
                {"provider": "groq-whisper", "status": "semantic_review", "transcript_sha256": "a" * 64},
                {"provider": "gemini-audio", "status": "technical_failure", "error_code": "PROVIDER_CAPACITY"},
            ],
        )
        with self.assertRaises(contract.AudioProductionContractError) as caught:
            resume_audio_production_contract_v2(
                root,
                extractor=self._extractor,
                groq_transcriber=lambda _audio: (_ for _ in ()).throw(AssertionError("groq must not rerun")),
                gemini_transcriber=lambda _audio: (_ for _ in ()).throw(RuntimeError("429 quota")),
            )
        self.assertIs(caught.exception.code, contract.AudioContractErrorCode.AUDIT_UNAVAILABLE)
        saved = json.loads((root / contract.AUDIT_FILENAME).read_text(encoding="utf-8"))
        by_provider = {item["provider"]: item for item in saved["attempts"]}
        self.assertEqual(by_provider["groq-whisper"]["status"], "semantic_review")
        self.assertEqual(by_provider["gemini-audio"]["status"], "technical_failure")
        self.assertEqual(saved["resume_provider_attempts"], 1)

    def test_both_technical_prior_can_accept_first_success_with_two_attempt_absolute_bound(self) -> None:
        temp, root, transcript = self._root()
        self.addCleanup(temp.cleanup)
        self._write_prior(
            root,
            transcript,
            [
                {"provider": "groq-whisper", "status": "technical_failure", "error_code": "PROVIDER_CAPACITY"},
                {"provider": "gemini-audio", "status": "technical_failure", "error_code": "PROVIDER_TRANSIENT"},
            ],
        )
        calls = {"gemini": 0}

        def gemini(_audio: Path) -> str:
            calls["gemini"] += 1
            return transcript

        result = resume_audio_production_contract_v2(
            root,
            extractor=self._extractor,
            groq_transcriber=lambda _audio: transcript,
            gemini_transcriber=gemini,
        )
        self.assertEqual(result["decision"], "pass")
        self.assertEqual(result["resume_provider_attempts"], 1)
        self.assertEqual(calls["gemini"], 0)
        self.assertLessEqual(result["resume_provider_attempts"], 2)


if __name__ == "__main__":
    unittest.main()
