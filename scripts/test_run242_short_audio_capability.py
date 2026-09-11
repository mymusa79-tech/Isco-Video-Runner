from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import audio_production_contract_v2 as contract
from scripts.short_finishing_capabilities import (
    ShortFinishingCapabilities,
    ShortFinishingCapabilityError,
    bind_short_finishing_capabilities,
)


class Run242ShortAudioCapabilityTests(unittest.TestCase):
    @staticmethod
    def _write_json(root: Path, name: str, value: object) -> None:
        (root / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def _base_short(self, root: Path, transcript: str) -> None:
        (root / "final.mp4").write_bytes(b"run242-final-short-audio" * 256)
        self._write_json(root, "plan.json", {"format": "moment"})
        self._write_json(
            root,
            "short-intelligence-pre-gold.json",
            {"voice": {"transcript": transcript}},
        )

    @staticmethod
    def _extract(_final: Path, audio: Path) -> None:
        audio.write_bytes(b"run242-flac-evidence" * 64)

    def test_run242_near_threshold_uses_scoped_gemini_capability(self) -> None:
        # 13/14 matching tokens reproduces the observed 0.928571 Groq recall while
        # keeping character similarity high enough that recall alone causes review.
        expected = "ا ب ج د ه و ز ح ط ي ك ل م ن"
        groq_actual = "ا ب ج د ه و ز ح ط ي ك ل م"
        comparison = contract.compare_transcripts(expected, groq_actual)
        self.assertEqual(comparison["token_recall"], 0.928571)
        self.assertEqual(comparison["thresholds"]["token_recall"], 0.93)
        self.assertEqual(comparison["decision"], "review")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._base_short(root, expected)
            capabilities = ShortFinishingCapabilities(
                gemini="run242-scoped-gemini",
                pexels="run242-pexels",
            )

            def gemini_fallback(_audio: Path) -> str:
                self.assertEqual(
                    contract._resolve_gemini_audit_key(),
                    "run242-scoped-gemini",
                )
                return expected

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("GEMINI_API_KEY", None)
                os.environ.pop("GEMINI_API_KEY_FILE", None)
                with bind_short_finishing_capabilities(capabilities):
                    result = contract.require_audio_production_contract_v2(
                        root,
                        extractor=self._extract,
                        groq_transcriber=lambda _audio: groq_actual,
                        gemini_transcriber=gemini_fallback,
                    )

        self.assertEqual(result["decision"], "pass")
        self.assertEqual(result["scope"], "short")
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["accepted_provider"], "gemini-audio")
        self.assertEqual(result["attempts"][0]["status"], "semantic_review")
        self.assertEqual(result["attempts"][0]["comparison"]["token_recall"], 0.928571)
        self.assertEqual(result["attempts"][1]["status"], "pass")

    def test_active_short_scope_never_falls_back_to_environment(self) -> None:
        capabilities = ShortFinishingCapabilities(gemini="", pexels="pexels")
        with patch.dict(os.environ, {"GEMINI_API_KEY": "stale-environment-key"}, clear=False):
            with bind_short_finishing_capabilities(capabilities):
                with self.assertRaisesRegex(
                    ShortFinishingCapabilityError,
                    "SHORT_AUDIO_GEMINI_CAPABILITY_MISSING",
                ):
                    contract._resolve_gemini_audit_key()

    def test_long_outside_short_scope_keeps_existing_environment_resolution(self) -> None:
        with patch.dict(os.environ, {"GEMINI_API_KEY": "long-existing-key"}, clear=False):
            self.assertEqual(contract._resolve_gemini_audit_key(), "long-existing-key")

    def test_short_scope_uses_capability_even_if_environment_contains_another_key(self) -> None:
        capabilities = ShortFinishingCapabilities(
            gemini="authoritative-short-key",
            pexels="pexels",
        )
        with patch.dict(os.environ, {"GEMINI_API_KEY": "wrong-or-stale-key"}, clear=False):
            with bind_short_finishing_capabilities(capabilities):
                self.assertEqual(
                    contract._resolve_gemini_audit_key(),
                    "authoritative-short-key",
                )


if __name__ == "__main__":
    unittest.main()
