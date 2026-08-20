from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.voice_mesh as voice_mesh


class PiperChunkingTests(unittest.TestCase):
    def test_short_text_stays_one_chunk(self) -> None:
        text = "هذا نص عربي قصير وواضح."
        self.assertEqual(voice_mesh._piper_chunks(text), [text])

    def test_long_text_prefers_sentence_boundaries_and_respects_limit(self) -> None:
        sentence = "هذه جملة عربية مكتملة للاختبار وتحتوي كلمات كافية."
        text = " ".join([sentence] * 20)
        chunks = voice_mesh._piper_chunks(text, max_chars=120)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(chunk) <= 120 for chunk in chunks))
        self.assertEqual(" ".join(chunks).split(), text.split())

    def test_single_oversized_sentence_falls_back_to_word_boundaries(self) -> None:
        text = " ".join(["كلمة"] * 80)
        chunks = voice_mesh._piper_chunks(text, max_chars=60)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 60 for chunk in chunks))
        self.assertEqual(" ".join(chunks).split(), text.split())

    def test_long_local_synthesis_uses_multiple_piper_pieces_then_one_concat(self) -> None:
        text = " ".join(["هذه جملة طويلة للاختبار."] * 30)
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "section.wav"
            created_parts: list[Path] = []

            def fake_piece(chunk: str, path: Path) -> Path:
                self.assertLessEqual(len(chunk), voice_mesh.PIPER_MAX_CHARS_PER_CHUNK)
                path.write_bytes(b"wav")
                created_parts.append(path)
                return path

            def fake_concat(parts: list[Path], destination: Path):
                self.assertGreater(len(parts), 1)
                self.assertTrue(all(part.exists() for part in parts))
                destination.write_bytes(b"joined")
                return destination

            with patch.object(voice_mesh, "_synthesize_piper_piece", side_effect=fake_piece) as synth_piece, \
                    patch.object(voice_mesh, "concat_audio", side_effect=fake_concat) as concat:
                result = voice_mesh._local(text, output)

            self.assertEqual(result, output)
            self.assertTrue(output.exists())
            self.assertGreater(synth_piece.call_count, 1)
            concat.assert_called_once()
            self.assertTrue(all(not part.exists() for part in created_parts))

    def test_short_local_synthesis_keeps_direct_single_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "short.wav"
            with patch.object(voice_mesh, "_synthesize_piper_piece", return_value=output) as piece, \
                    patch.object(voice_mesh, "concat_audio") as concat:
                result = voice_mesh._local("نص قصير", output)

            self.assertEqual(result, output)
            piece.assert_called_once_with("نص قصير", output)
            concat.assert_not_called()

    def test_empty_local_transcript_fails_closed_before_piper(self) -> None:
        with patch.object(voice_mesh, "_synthesize_piper_piece") as piece:
            with self.assertRaisesRegex(RuntimeError, "voice_local_empty_transcript"):
                voice_mesh._local("   ", Path("empty.wav"))
        piece.assert_not_called()


if __name__ == "__main__":
    unittest.main()
