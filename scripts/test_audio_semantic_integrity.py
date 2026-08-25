from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import audio_semantic_integrity as integrity
from scripts import groq_audio_audit


class AudioSemanticIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        integrity.reset_audio_semantic_integrity_state_for_tests()

    def tearDown(self) -> None:
        integrity.reset_audio_semantic_integrity_state_for_tests()

    def _start(self) -> None:
        with patch.dict(os.environ, {"ISCO_PRODUCTION_ID": "v4:test:1"}, clear=False):
            integrity._begin_run()

    def _fixture(self, root: Path, narrations: list[str]) -> tuple[Path, list[Path], Path, Path]:
        out = root / "output"
        (out / "audio").mkdir(parents=True)
        (out / "plan.json").write_text(
            json.dumps(
                {
                    "format": "film",
                    "sections": [
                        {"id": f"s{index}", "narration": text}
                        for index, text in enumerate(narrations, 1)
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (out / "quality-final.json").write_text(
            json.dumps({"audio_ok": True, "av_sync_ok": True}), encoding="utf-8"
        )
        section_paths = [out / "audio" / f"{index:02d}.wav" for index in range(1, len(narrations) + 1)]
        narration_requested = out / "narration.wav"
        final = out / "final.mp4"
        return out, section_paths, narration_requested, final

    def _bind_success(self, root: Path, narrations: list[str]):
        self._start()
        out, section_paths, narration_requested, final = self._fixture(root, narrations)

        def fake_tts(*args, **kwargs):
            path = Path(kwargs["output"])
            path.write_bytes(("AUDIO:" + kwargs["transcript"]).encode("utf-8"))
            return path

        tts = integrity._wrap_tts(fake_tts)
        for index, (text, path) in enumerate(zip(narrations, section_paths), 1):
            tts(
                None,
                None,
                None,
                task_id=f"TTS_SECTION_{index:02d}",
                api_key="x",
                transcript=text,
                output=path,
                model="m",
                voice="v",
                style="s",
            )

        def mastering_concat(inputs, output):
            requested = Path(output)
            requested.write_bytes(b"|".join(Path(item).read_bytes() for item in inputs))
            mastered = requested.with_name("narration-mastered.wav")
            mastered.write_bytes(b"MASTERED:" + requested.read_bytes())
            return mastered

        concat = integrity._wrap_concat_audio(mastering_concat)
        narration = concat(section_paths, narration_requested)

        def authorized_mux(video, narration, output, *args, **kwargs):
            del video, args, kwargs
            Path(output).write_bytes(b"FINAL:" + Path(narration).read_bytes())
            return Path(output)

        mux = integrity._wrap_mux(authorized_mux)
        mux(out / "picture.mp4", narration, final, music=None)
        return out, section_paths, Path(narration), final

    def test_full_binding_passes_with_authorized_mastering_result_and_final_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out, section_paths, narration, final = self._bind_success(
                Path(tmp), ["النص الأول المعتمد", "النص الثاني المعتمد"]
            )
            result = integrity.require_audio_semantic_integrity(out)
            self.assertEqual(result["decision"], "pass")
            self.assertEqual(result["groq_semantic_audit"], "observe_only")
            self.assertTrue(result["checks"]["approved_plan_to_tts"])
            self.assertTrue(result["checks"]["ordered_authorized_narration_transform"])
            self.assertEqual(Path(result["narration"]["path"]), narration.resolve())
            self.assertEqual(Path(result["final_mux"]["final_path"]), final.resolve())
            self.assertEqual(len(result["sections"]), 2)
            self.assertTrue(all(Path(path).is_file() for path in section_paths))
            saved = json.loads((out / integrity.AUDIT_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(saved["decision"], "pass")

    def test_plan_transcript_change_after_tts_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out, *_ = self._bind_success(Path(tmp), ["النص المعتمد الأصلي"])
            (out / "plan.json").write_text(
                json.dumps(
                    {"format": "film", "sections": [{"id": "s1", "narration": "نص تم استبداله"}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "transcript_mismatch"):
                integrity.require_audio_semantic_integrity(out)
            audit = json.loads((out / integrity.AUDIT_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(audit["decision"], "block")

    def test_tts_audio_tamper_after_generation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out, section_paths, *_ = self._bind_success(Path(tmp), ["نص ثابت"])
            section_paths[0].write_bytes(b"tampered-audio")
            with self.assertRaisesRegex(RuntimeError, "section_audio_mismatch"):
                integrity.require_audio_semantic_integrity(out)

    def test_concat_rejects_uncertified_or_reordered_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._start()
            out, section_paths, narration_requested, _ = self._fixture(root, ["أ", "ب"])

            def fake_tts(*args, **kwargs):
                path = Path(kwargs["output"])
                path.write_bytes(kwargs["transcript"].encode("utf-8"))
                return path

            tts = integrity._wrap_tts(fake_tts)
            for index, (text, path) in enumerate(zip(["أ", "ب"], section_paths), 1):
                tts(None, None, None, task_id=f"TTS_SECTION_{index:02d}", transcript=text, output=path)

            def concat(inputs, output):
                Path(output).write_bytes(b"".join(Path(item).read_bytes() for item in inputs))
                return Path(output)

            bound = integrity._wrap_concat_audio(concat)
            # The concat wrapper records the actual order; the final plan check must
            # reject an order that differs from section/task order.
            narration = bound(list(reversed(section_paths)), narration_requested)

            def mux(video, narration, output, **kwargs):
                del video, kwargs
                Path(output).write_bytes(Path(narration).read_bytes())
                return Path(output)

            integrity._wrap_mux(mux)(out / "p.mp4", narration, out / "final.mp4")
            with self.assertRaisesRegex(RuntimeError, "concat_order_mismatch"):
                integrity.require_audio_semantic_integrity(out)

    def test_mux_rejects_narration_not_returned_by_authorized_concat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._start()
            rogue = root / "rogue.wav"
            rogue.write_bytes(b"rogue")

            def mux(video, narration, output, **kwargs):
                del video, narration, kwargs
                Path(output).write_bytes(b"final")
                return Path(output)

            with self.assertRaisesRegex(RuntimeError, "uncertified_narration_at_mux"):
                integrity._wrap_mux(mux)(root / "p.mp4", rogue, root / "final.mp4")

    def test_final_artifact_tamper_after_mux_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out, _, _, final = self._bind_success(Path(tmp), ["النص المعتمد"])
            final.write_bytes(b"replacement-final")
            with self.assertRaisesRegex(RuntimeError, "final_hash_mismatch"):
                integrity.require_audio_semantic_integrity(out)

    def test_quality_final_audio_or_av_sync_failure_is_not_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out, *_ = self._bind_success(Path(tmp), ["النص المعتمد"])
            (out / "quality-final.json").write_text(
                json.dumps({"audio_ok": True, "av_sync_ok": False}), encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "final_audio_quality_not_certified"):
                integrity.require_audio_semantic_integrity(out)

    def test_moment_is_explicitly_not_applicable_without_tts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            out.mkdir()
            (out / "plan.json").write_text(
                json.dumps({"format": "moment", "sections": [{"id": "s1", "narration": ""}]}),
                encoding="utf-8",
            )
            self._start()
            result = integrity.require_audio_semantic_integrity(out)
            self.assertEqual(result["decision"], "not_applicable")

    def test_existing_groq_audit_remains_observe_only(self) -> None:
        self.assertEqual(groq_audio_audit.MODE, "observe_only")
        source = Path(groq_audio_audit.__file__).read_text(encoding="utf-8")
        self.assertIn('"enforcement": "disabled"', source)
        self.assertNotIn("require_audio_semantic_integrity", source)


if __name__ == "__main__":
    unittest.main()
