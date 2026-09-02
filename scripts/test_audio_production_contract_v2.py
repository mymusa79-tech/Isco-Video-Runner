from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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
            self.assertEqual(result["ownership"]["semantic_fidelity"], contract.CONTRACT_ID)
            self.assertEqual(result["ownership"]["final_compliance_repair"], "audio_producer_repair_lifecycle")

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

    def test_semantic_review_plus_technical_failure_is_unavailable_not_false_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._base_long(root, "لا تتراجع عن قرارك")
            with self.assertRaisesRegex(contract.AudioProductionContractError, "AUDIT_UNAVAILABLE"):
                contract.require_audio_production_contract_v2(
                    root,
                    extractor=self._extract,
                    groq_transcriber=lambda _audio: "تراجع عن قرارك",
                    gemini_transcriber=lambda _audio: (_ for _ in ()).throw(TimeoutError("timed out")),
                )
            audit = json.loads((root / contract.AUDIT_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(audit["error_code"], "AUDIT_UNAVAILABLE")
            self.assertEqual(audit["attempts"][0]["status"], "semantic_review")
            self.assertEqual(audit["attempts"][1]["error_code"], "PROVIDER_TRANSIENT")

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

    def test_auth_failure_is_classified_separately(self) -> None:
        self.assertEqual(
            contract._classify_provider_failure(RuntimeError("HTTP 401 unauthorized")),
            contract.AudioContractErrorCode.PROVIDER_AUTH,
        )
        self.assertEqual(
            contract._classify_provider_failure(RuntimeError("HTTP 403 forbidden")),
            contract.AudioContractErrorCode.PROVIDER_AUTH,
        )

    def test_final_bytes_cannot_change_during_provider_audit(self) -> None:
        expected = "لا تستسلم عندما يصبح الطريق صعبا"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._base_long(root, expected)

            def mutate_and_pass(_audio: Path) -> str:
                with (root / "final.mp4").open("ab") as handle:
                    handle.write(b"mutation")
                return expected

            with self.assertRaisesRegex(contract.AudioProductionContractError, "FINAL_ARTIFACT_INVALID"):
                contract.require_audio_production_contract_v2(
                    root,
                    extractor=self._extract,
                    groq_transcriber=mutate_and_pass,
                    gemini_transcriber=lambda _audio: self.fail("fallback must not run after byte drift"),
                )

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

    def test_gemini_large_audio_uses_files_api_and_deletes_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio = Path(temp_dir) / "large.flac"
            with audio.open("wb") as handle:
                handle.truncate(contract.MAX_INLINE_AUDIO_BYTES + 1)

            events: list[tuple[str, object]] = []

            class Files:
                def upload(self, *, file: str):
                    events.append(("upload", file))
                    return SimpleNamespace(name="files/audio-contract-test")

                def delete(self, *, name: str):
                    events.append(("delete", name))

            class Models:
                def generate_content(self, *, model: str, contents: list[object]):
                    events.append(("generate", model))
                    self.last_contents = contents
                    return SimpleNamespace(text="نص عربي مطابق")

            class Part:
                @staticmethod
                def from_bytes(**_kwargs):
                    raise AssertionError("large audio must not use inline bytes")

            client = SimpleNamespace(files=Files(), models=Models())
            types_module = SimpleNamespace(Part=Part)
            result = contract._gemini_transcribe_with_client(
                audio,
                client=client,
                types_module=types_module,
                model="gemini-3.7-flash",
            )
            self.assertEqual(result, "نص عربي مطابق")
            self.assertEqual(events[0][0], "upload")
            self.assertIn(("generate", "gemini-3.7-flash"), events)
            self.assertEqual(events[-1], ("delete", "files/audio-contract-test"))

    def test_gemini_small_audio_stays_inline_without_files_api(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio = Path(temp_dir) / "small.flac"
            audio.write_bytes(b"small-audio")

            class Files:
                def upload(self, **_kwargs):
                    raise AssertionError("small audio must remain inline")

                def delete(self, **_kwargs):
                    raise AssertionError("no upload means no delete")

            class Models:
                def generate_content(self, *, model: str, contents: list[object]):
                    self.contents = contents
                    return SimpleNamespace(text="نص")

            class Part:
                @staticmethod
                def from_bytes(*, data: bytes, mime_type: str):
                    return {"data_len": len(data), "mime_type": mime_type}

            client = SimpleNamespace(files=Files(), models=Models())
            result = contract._gemini_transcribe_with_client(
                audio,
                client=client,
                types_module=SimpleNamespace(Part=Part),
                model="gemini-3.7-flash",
            )
            self.assertEqual(result, "نص")


if __name__ == "__main__":
    unittest.main()
