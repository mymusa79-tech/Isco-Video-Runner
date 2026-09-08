from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import audio_qc_pending_checkpoint_v1 as pending
from scripts.audio_production_contract_v2 import (
    AUDIT_FILENAME,
    AudioContractErrorCode,
    AudioProductionContractError,
)
from scripts.audio_retention_qc import REPORT_FILENAME as RETENTION_FILENAME
from scripts.audio_semantic_resume_state_v1 import FILENAME as SEMANTIC_FILENAME


class AudioQCPendingCheckpointV1Tests(unittest.TestCase):
    def _root(self) -> tuple[tempfile.TemporaryDirectory[str], Path, str]:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        (root / "final.mp4").write_bytes(b"final-media" * 400)
        final_sha = pending._sha256_file(root / "final.mp4")
        (root / "plan.json").write_text(json.dumps({"format": "moment"}), encoding="utf-8")
        (root / AUDIT_FILENAME).write_text(
            json.dumps(
                {
                    "contract_id": "audio.production.v2",
                    "decision": "block",
                    "error_code": "AUDIT_UNAVAILABLE",
                    "final_sha256": final_sha,
                    "attempts": [
                        {"provider": "groq-whisper", "status": "semantic_review"},
                        {"provider": "gemini-audio", "status": "technical_failure", "error_code": "PROVIDER_CAPACITY"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        (root / RETENTION_FILENAME).write_text("{}", encoding="utf-8")
        (root / SEMANTIC_FILENAME).write_text("{}", encoding="utf-8")
        return temp, root, final_sha

    def _eligible_patches(self, root: Path, final_sha: str):
        semantic = {
            "contract_id": "audio.semantic.resume-state.v1",
            "final": {"sha256": final_sha},
        }
        return (
            patch.object(
                pending,
                "require_audio_producer_certificate",
                return_value={"phase": "short_finished", "decision": "pass", "final_sha256": final_sha},
            ),
            patch.object(
                pending,
                "_require_retention_binding",
                return_value={"status": "pass", "scope": "short"},
            ),
            patch.object(pending, "export_audio_semantic_resume_state", return_value=semantic),
            patch.object(
                pending,
                "_pending_production_record",
                return_value={"output": "output/test/final.mp4", "release_status": "pending"},
            ),
            patch.object(pending, "_output_key", return_value="output/test/final.mp4"),
        )

    def test_only_exact_audit_unavailable_type_is_resumable(self) -> None:
        self.assertTrue(
            pending.is_audio_qc_pending_error(
                AudioProductionContractError(AudioContractErrorCode.AUDIT_UNAVAILABLE, "provider unavailable")
            )
        )
        self.assertFalse(
            pending.is_audio_qc_pending_error(
                AudioProductionContractError(AudioContractErrorCode.SEMANTIC_MISMATCH, "confirmed mismatch")
            )
        )
        self.assertFalse(pending.is_audio_qc_pending_error(RuntimeError("AUDIT_UNAVAILABLE provider unavailable")))

    def test_confirmed_semantic_mismatch_never_writes_pending_checkpoint(self) -> None:
        temp, root, _ = self._root()
        self.addCleanup(temp.cleanup)
        exc = AudioProductionContractError(AudioContractErrorCode.SEMANTIC_MISMATCH, "confirmed")
        self.assertIsNone(pending.capture_audio_qc_pending_checkpoint(root, exc))
        self.assertFalse((root / pending.FILENAME).exists())

    def test_eligible_unavailable_captures_exact_final_and_nonaccepted_state(self) -> None:
        temp, root, final_sha = self._root()
        self.addCleanup(temp.cleanup)
        # The exporter is patched but capture hashes its durable file; keep a realistic file.
        (root / SEMANTIC_FILENAME).write_text(
            json.dumps({"contract_id": "audio.semantic.resume-state.v1", "final": {"sha256": final_sha}}),
            encoding="utf-8",
        )
        (root / RETENTION_FILENAME).write_text(
            json.dumps({"status": "pass", "final": {"sha256": final_sha}}),
            encoding="utf-8",
        )
        env = {
            "GITHUB_SHA": "a" * 40,
            "ISCO_ENGINE_SHA": "b" * 40,
            "GITHUB_RUN_ID": "22600",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_RUN_NUMBER": "226",
            "ISCO_PRODUCTION_ID": "v4:22600:1",
            "ISCO_RELEASE_TAG_OVERRIDE": "video-226",
        }
        patches = self._eligible_patches(root, final_sha)
        with patch.dict(os.environ, env, clear=False):
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                path = pending.capture_audio_qc_pending_checkpoint(
                    root,
                    AudioProductionContractError(AudioContractErrorCode.AUDIT_UNAVAILABLE, "transient"),
                )
        self.assertEqual(path, root / pending.FILENAME)
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(doc["status"], pending.STATUS)
        self.assertEqual(doc["final"]["sha256"], final_sha)
        self.assertFalse(doc["release_allowed"])
        self.assertTrue(doc["resumable"])
        self.assertFalse(doc["production_state"]["accepted"])
        self.assertFalse(doc["retry_policy"]["tts_allowed"])
        self.assertFalse(doc["retry_policy"]["parent_rerender_allowed"])
        self.assertFalse(doc["retry_policy"]["semantic_mismatch_is_resumable"])
        self.assertEqual(doc["retry_policy"]["resume_execution_limit"], 1)

    def test_final_hash_drift_refuses_checkpoint(self) -> None:
        temp, root, final_sha = self._root()
        self.addCleanup(temp.cleanup)
        audit = json.loads((root / AUDIT_FILENAME).read_text(encoding="utf-8"))
        audit["final_sha256"] = "0" * 64
        (root / AUDIT_FILENAME).write_text(json.dumps(audit), encoding="utf-8")
        with self.assertRaises(pending.AudioQCPendingCheckpointError):
            pending.capture_audio_qc_pending_checkpoint(
                root,
                AudioProductionContractError(AudioContractErrorCode.AUDIT_UNAVAILABLE, "transient"),
            )


if __name__ == "__main__":
    unittest.main()
