from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import audio_semantic_integrity as integrity
from scripts.audio_semantic_resume_state_v1 import (
    AudioSemanticResumeStateError,
    export_audio_semantic_resume_state,
    restore_audio_semantic_resume_state,
)


class AudioSemanticResumeStateV1Tests(unittest.TestCase):
    def tearDown(self) -> None:
        integrity.reset_audio_semantic_integrity_state_for_tests()

    def _long_root(self) -> tuple[tempfile.TemporaryDirectory[str], Path, str]:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        production_id = "v4:99123:2"
        narrations = ["النص الأول واضح", "النص الثاني واضح"]
        (root / "plan.json").write_text(
            json.dumps(
                {
                    "format": "film",
                    "sections": [
                        {"id": "s1", "narration": narrations[0]},
                        {"id": "s2", "narration": narrations[1]},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (root / "quality-final.json").write_text(
            json.dumps({"audio_ok": True, "av_sync_ok": True}),
            encoding="utf-8",
        )
        audio_dir = root / "audio"
        audio_dir.mkdir()
        paths = []
        records = []
        for index, narration in enumerate(narrations, 1):
            path = audio_dir / f"{index:02d}.wav"
            path.write_bytes((f"wav-{index}".encode()) * 100)
            paths.append(path)
            records.append(
                integrity.TtsSectionBinding(
                    task_id=f"TTS_SECTION_{index:02d}",
                    transcript_sha256=integrity._sha256_text(narration),
                    transcript_utf8_bytes=len(narration.encode("utf-8")),
                    audio_path=integrity._path_key(path),
                    audio_sha256=integrity._sha256_file(path),
                    audio_bytes=path.stat().st_size,
                )
            )
        narration_path = root / "narration.wav"
        narration_path.write_bytes(b"narration" * 200)
        narration_record = integrity.NarrationBinding(
            path=integrity._path_key(narration_path),
            sha256=integrity._sha256_file(narration_path),
            byte_length=narration_path.stat().st_size,
            ordered_task_ids=tuple(item.task_id for item in records),
            ordered_transcript_sha256=tuple(item.transcript_sha256 for item in records),
            ordered_audio_sha256=tuple(item.audio_sha256 for item in records),
            authorized_transform_sha256="c" * 64,
        )
        final_path = root / "final.mp4"
        final_path.write_bytes(b"final" * 1000)
        final_record = integrity.FinalMuxBinding(
            final_path=integrity._path_key(final_path),
            final_sha256=integrity._sha256_file(final_path),
            final_bytes=final_path.stat().st_size,
            narration_path=narration_record.path,
            narration_sha256=narration_record.sha256,
            authorized_mux_chain_sha256="d" * 64,
        )

        integrity.reset_audio_semantic_integrity_state_for_tests()
        integrity._production_id = production_id
        for record in records:
            integrity._tts_by_task[record.task_id] = record
            integrity._tts_by_path[record.audio_path] = record
        integrity._narration_by_path[narration_record.path] = narration_record
        integrity._final_by_path[final_record.final_path] = final_record
        return temp, root, production_id

    def test_long_export_restore_replays_exact_provenance_without_tts(self) -> None:
        temp, root, production_id = self._long_root()
        self.addCleanup(temp.cleanup)
        with patch.dict(os.environ, {"ISCO_PRODUCTION_ID": production_id}, clear=False):
            state = export_audio_semantic_resume_state(root)
            self.assertEqual(state["mode"], "long_provenance_replay")
            integrity.reset_audio_semantic_integrity_state_for_tests()
            restore_audio_semantic_resume_state(root, expected_production_id=production_id)
            report = integrity.require_audio_semantic_integrity(root)
        self.assertEqual(report["decision"], "pass")
        self.assertEqual(len(report["sections"]), 2)
        self.assertTrue(report["checks"]["approved_plan_to_tts"])

    def test_mutated_long_tts_byte_refuses_restore(self) -> None:
        temp, root, production_id = self._long_root()
        self.addCleanup(temp.cleanup)
        with patch.dict(os.environ, {"ISCO_PRODUCTION_ID": production_id}, clear=False):
            export_audio_semantic_resume_state(root)
        (root / "audio" / "01.wav").write_bytes(b"mutated")
        integrity.reset_audio_semantic_integrity_state_for_tests()
        with self.assertRaises(AudioSemanticResumeStateError):
            restore_audio_semantic_resume_state(root, expected_production_id=production_id)

    def test_moment_resume_never_fabricates_engine_tts_provenance(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        production_id = "v4:123:1"
        (root / "plan.json").write_text(json.dumps({"format": "moment", "sections": [{"id": "s1"}]}), encoding="utf-8")
        (root / "final.mp4").write_bytes(b"short-final" * 300)
        integrity.reset_audio_semantic_integrity_state_for_tests()
        integrity._production_id = production_id
        with patch.dict(os.environ, {"ISCO_PRODUCTION_ID": production_id}, clear=False):
            state = export_audio_semantic_resume_state(root)
        self.assertEqual(state["mode"], "moment_final_gate_not_applicable")
        self.assertEqual(state["tts_sections"], [])
        integrity.reset_audio_semantic_integrity_state_for_tests()
        restore_audio_semantic_resume_state(root, expected_production_id=production_id)
        self.assertEqual(integrity._production_id, production_id)
        self.assertFalse(integrity._tts_by_task)


if __name__ == "__main__":
    unittest.main()
