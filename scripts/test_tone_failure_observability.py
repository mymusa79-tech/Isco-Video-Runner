from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import scripts.run_v3_voice as run_v3_voice


class _chdir:
    def __init__(self, path: str | Path):
        self._path = str(path)

    def __enter__(self):
        self._original = os.getcwd()
        os.chdir(self._path)
        return self

    def __exit__(self, *exc_info):
        os.chdir(self._original)


class ToneFailureObservabilityTests(unittest.TestCase):
    def test_attaches_full_tone_audit_without_losing_precheck_fields(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            precheck = {"tone_quality_status": "block", "novelty_flags": []}
            tone = {
                "schema_version": 1,
                "audit": "tone_quality",
                "provider": "gemini",
                "raw_result": {
                    "status": "block",
                    "naturalness_flags": ["dialogue sounds artificial"],
                },
                "attempts": [{"provider": "gemini", "outcome": "content_blocked", "detail": None}],
                "validation": "valid",
                "error": None,
                "error_message": None,
                "status": "block",
                "preachiness_flags": [],
                "cultural_dignity_flags": [],
                "naturalness_flags": ["dialogue sounds artificial"],
                "narrative_format_flags": ["speaker turns are not natural"],
                "unverified_religious_quote_flags": [],
                "notes": ["keep the full reviewer evidence"],
            }
            (out / "quality-precheck.json").write_text(json.dumps(precheck), encoding="utf-8")
            (out / "tone-quality-audit.json").write_text(json.dumps(tone), encoding="utf-8")

            run_v3_voice._attach_failure_tone_diagnostics(out)

            saved = json.loads((out / "quality-precheck.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["tone_quality_status"], "block")
            self.assertEqual(saved["novelty_flags"], [])
            self.assertEqual(saved["tone_quality_audit"], tone)

    def test_malformed_tone_sidecar_never_masks_original_failure_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            original = {"tone_quality_status": "block"}
            (out / "quality-precheck.json").write_text(json.dumps(original), encoding="utf-8")
            (out / "tone-quality-audit.json").write_text("{not-json", encoding="utf-8")

            run_v3_voice._attach_failure_tone_diagnostics(out)

            saved = json.loads((out / "quality-precheck.json").read_text(encoding="utf-8"))
            self.assertEqual(saved, original)

    def test_main_failure_path_enriches_uploaded_precheck_before_reraising(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            request = root / "request.json"
            request.write_text(json.dumps({"topic": "x", "format": "film"}), encoding="utf-8")
            out = root / "output" / "run-1"
            out.mkdir(parents=True)
            (out / "quality-precheck.json").write_text(
                json.dumps({"tone_quality_status": "block"}), encoding="utf-8"
            )
            tone = {
                "status": "block",
                "provider": "gemini",
                "validation": "valid",
                "raw_result": {"status": "block", "narrative_format_flags": ["artificial dialogue"]},
                "narrative_format_flags": ["artificial dialogue"],
            }
            (out / "tone-quality-audit.json").write_text(json.dumps(tone), encoding="utf-8")

            installers = [
                "install_entrypoint_planning_contracts",
                "install_runtime_closure",
                "install_post_runtime_planning_contracts",
                "install_tts_runtime_port",
                "install_cinematic_runtime_port",
                "install_opening_feasibility_guard",
                "start_progress",
                "install_progress_hooks",
            ]
            with _chdir(root), ExitStack() as stack:
                for name in installers:
                    stack.enter_context(patch.object(run_v3_voice, name))
                stack.enter_context(patch.object(run_v3_voice, "secret", return_value="gemini-key"))
                stack.enter_context(
                    patch.object(run_v3_voice.orchestrator, "produce", side_effect=RuntimeError("tone blocked"))
                )
                stack.enter_context(patch.object(run_v3_voice, "write_planning_telemetry"))
                stack.enter_context(
                    patch.dict(
                        os.environ,
                        {
                            "REQUEST_FILE": str(request),
                            "GEMINI_CONTENT_MODEL": "gemini-3.7-flash",
                            "GEMINI_TTS_MODEL": "gemini-3.1-flash-tts-preview",
                        },
                        clear=False,
                    )
                )

                with self.assertRaisesRegex(RuntimeError, "tone blocked"):
                    run_v3_voice.main()

            saved = json.loads((out / "quality-precheck.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["tone_quality_audit"], tone)


if __name__ == "__main__":
    unittest.main()
