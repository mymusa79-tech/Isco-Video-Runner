from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import final_master_acceptance_v2 as acceptance
from scripts import final_master_qc
from scripts import final_qc_observer_durability as durability
from scripts import groq_audio_audit
from scripts import voice_identity_observer


_TOOLS = {
    "ffmpeg": {"first_line": "ffmpeg-test", "sha256": "f" * 64},
    "ffprobe": {"first_line": "ffprobe-test", "sha256": "e" * 64},
}

_ACCEPTANCE_PROBE = {
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "profile": "High",
            "field_order": "progressive",
            "color_transfer": "bt709",
            "color_primaries": "bt709",
            "color_space": "bt709",
        },
        {
            "codec_type": "audio",
            "codec_name": "aac",
            "profile": "LC",
            "sample_rate": "48000",
            "channels": 2,
        },
    ]
}


class FinalQcObserverDurabilityTests(unittest.TestCase):
    @staticmethod
    def _env(root: Path, *, run_id: str = "100") -> dict[str, str]:
        return {
            "ISCO_TTS_CACHE_PATH": str(root / "cache"),
            "ISCO_ENGINE_SHA": "a" * 40,
            "ISCO_APPROVED_BRIEF_SHA256": "b" * 64,
            "GITHUB_RUN_ID": run_id,
        }

    @staticmethod
    def _final_inputs(root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "final.mp4").write_bytes(b"V" * 4096)
        (root / "plan.json").write_text(json.dumps({"format": "film"}), encoding="utf-8")
        (root / "quality-final.json").write_text(json.dumps({"format": "film"}), encoding="utf-8")
        (root / "visual-timeline.json").write_text(
            json.dumps({"duration_seconds": 10.0}), encoding="utf-8"
        )

    @staticmethod
    def _pass_report() -> dict:
        return {
            "schema_version": final_master_qc.SCHEMA_VERSION,
            "status": "pass",
            "production_stage": "post_render_pre_gold_acceptance",
            "full_decode_ok": True,
            "full_decode_timed_out": False,
            "final_media_mutated": False,
            "blocking_findings": [],
        }

    def setUp(self) -> None:
        durability._FFMPEG_IDENTITY = None

    def test_final_qc_pass_is_reused_only_for_exact_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "run"
            self._final_inputs(out)
            calls = {"qc": 0}

            def qc(output_dir: Path):
                calls["qc"] += 1
                report = self._pass_report()
                (Path(output_dir) / "final-master-qc.json").write_text(
                    json.dumps(report), encoding="utf-8"
                )
                return report

            with patch.dict(os.environ, self._env(root), clear=False), patch.object(
                durability, "_media_tools_identity", return_value=_TOOLS
            ), patch.object(
                acceptance, "probe", return_value=_ACCEPTANCE_PROBE
            ), patch.object(
                acceptance, "_mp4_fast_start", return_value=(True, ["ftyp", "moov", "mdat"])
            ):
                first = durability.run_final_master_qc_durable(out, original=qc)
                self.assertEqual(first["status"], "pass")
                self.assertEqual(first["acceptance_contract"]["contract_id"], "final.master.acceptance.v2")
                (out / "final-master-qc.json").unlink()
                second = durability.run_final_master_qc_durable(out, original=qc)
                self.assertEqual(second["status"], "pass")
                self.assertEqual(second["acceptance_contract"]["contract_id"], "final.master.acceptance.v2")
                self.assertEqual(calls["qc"], 1)

                (out / "final.mp4").write_bytes(b"W" * 4096)
                durability.run_final_master_qc_durable(out, original=qc)
                self.assertEqual(calls["qc"], 2)

    def test_final_qc_block_is_never_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "run"
            self._final_inputs(out)
            calls = {"qc": 0}

            def qc(output_dir: Path):
                calls["qc"] += 1
                report = {**self._pass_report(), "status": "block", "blocking_findings": ["x"]}
                (Path(output_dir) / "final-master-qc.json").write_text(
                    json.dumps(report), encoding="utf-8"
                )
                raise final_master_qc.FinalMasterQCError("blocked")

            with patch.dict(os.environ, self._env(root), clear=False), patch.object(
                durability, "_media_tools_identity", return_value=_TOOLS
            ):
                for _ in range(2):
                    with self.assertRaises(final_master_qc.FinalMasterQCError):
                        durability.run_final_master_qc_durable(out, original=qc)
                self.assertEqual(calls["qc"], 2)
                cache = Path(self._env(root)["ISCO_TTS_CACHE_PATH"]) / "final-qc"
                self.assertFalse(cache.exists())

    def test_groq_success_reuses_only_within_same_github_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "run"
            self._final_inputs(out)
            calls = {"groq": 0}

            def observer(output_dir: Path, *, api_key, model):
                del api_key
                calls["groq"] += 1
                document = {
                    "schema_version": 1,
                    "mode": groq_audio_audit.MODE,
                    "decision": "pass",
                    "groq_governor": {"status": "ok"},
                    "transcription": {"model": model, "text": "ok"},
                }
                (Path(output_dir) / groq_audio_audit.AUDIT_FILENAME).write_text(
                    json.dumps(document), encoding="utf-8"
                )
                return document

            with patch.object(durability, "_media_tools_identity", return_value=_TOOLS):
                with patch.dict(os.environ, self._env(root, run_id="100"), clear=False):
                    durability.run_groq_audio_audit_durable(out, api_key="x", original=observer)
                    (out / groq_audio_audit.AUDIT_FILENAME).unlink()
                    durability.run_groq_audio_audit_durable(out, api_key="x", original=observer)
                    self.assertEqual(calls["groq"], 1)
                with patch.dict(os.environ, self._env(root, run_id="101"), clear=False):
                    durability.run_groq_audio_audit_durable(out, api_key="x", original=observer)
                    self.assertEqual(calls["groq"], 2)

    def test_groq_error_or_rate_limit_is_never_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "run"
            self._final_inputs(out)
            calls = {"groq": 0}

            def observer(output_dir: Path, *, api_key, model):
                del api_key, model
                calls["groq"] += 1
                document = {
                    "schema_version": 1,
                    "mode": groq_audio_audit.MODE,
                    "decision": "audit_skipped",
                    "groq_governor": {"status": "rate_limited"},
                    "audit_error": "429",
                }
                (Path(output_dir) / groq_audio_audit.AUDIT_FILENAME).write_text(
                    json.dumps(document), encoding="utf-8"
                )
                return document

            with patch.dict(os.environ, self._env(root), clear=False), patch.object(
                durability, "_media_tools_identity", return_value=_TOOLS
            ):
                durability.run_groq_audio_audit_durable(out, api_key="x", original=observer)
                durability.run_groq_audio_audit_durable(out, api_key="x", original=observer)
                self.assertEqual(calls["groq"], 2)

    def test_voice_exact_entry_skips_second_local_embedding_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "run" / "audio" / "01.wav"
            audio.parent.mkdir(parents=True)
            audio.write_bytes(b"A" * 4096)
            audit = root / "run" / voice_identity_observer.AUDIT_FILENAME
            calls = {"observe": 0}
            original = voice_identity_observer.observe_output

            def fake_observe(*, task_id, transcript, output, model, requested_voice):
                del transcript
                calls["observe"] += 1
                entry = {
                    "task_id": task_id,
                    "section": "01",
                    "provider": "gemini",
                    "model": model,
                    "requested_voice": requested_voice,
                    "dialogue_mode": False,
                    "fallback_used": False,
                    "reference_profiles": ["primary"],
                    "mode": "observe_only",
                    "decision": "uncalibrated",
                }
                voice_identity_observer._append_entry(audit, entry)

            try:
                voice_identity_observer.observe_output = fake_observe
                with patch.dict(os.environ, self._env(root), clear=False), patch.object(
                    durability, "_package_version", return_value="test"
                ), patch.object(
                    durability.voice_mesh, "peek_voice_provenance",
                    return_value={"provider": "gemini", "fallback_used": False},
                ), patch.object(
                    durability.voice_mesh, "consume_voice_provenance",
                    return_value={"provider": "gemini", "fallback_used": False},
                ):
                    durability._install_voice_observer_durability()
                    wrapped = voice_identity_observer.observe_output
                    wrapped(
                        task_id="TTS_SECTION_01",
                        transcript="مرحبا",
                        output=audio,
                        model="tts-model",
                        requested_voice="voice",
                    )
                    wrapped(
                        task_id="TTS_SECTION_01",
                        transcript="مرحبا",
                        output=audio,
                        model="tts-model",
                        requested_voice="voice",
                    )
                    self.assertEqual(calls["observe"], 1)
                    document = json.loads(audit.read_text(encoding="utf-8"))
                    self.assertEqual(len(document["sections"]), 2)
            finally:
                voice_identity_observer.observe_output = original


if __name__ == "__main__":
    unittest.main()
