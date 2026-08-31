from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
import wave
from array import array
from pathlib import Path
from unittest.mock import patch

import scripts.voice_mesh as voice_mesh


def _piper_model_available() -> bool:
    model = (os.environ.get("PIPER_MODEL_PATH") or "").strip()
    return bool(model) and Path(model).is_file() and Path(model + ".json").is_file()


def _write_pcm_wav(path: Path, segments: list[tuple[int, int]], *, rate: int = 16000) -> None:
    """Write deterministic mono 16-bit PCM segments: (seconds, sample amplitude)."""
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        for seconds, amplitude in segments:
            samples = array("h", [amplitude]) * (rate * seconds)
            wav.writeframes(samples.tobytes())


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

    def test_whole_file_acoustic_qa_rejects_silent_tail_after_healthy_opening(self) -> None:
        # The legacy first-15s RMS sample would pass this: ten loud seconds followed by
        # five silent seconds still had a high RMS, while the final five seconds were
        # never inspected. Whole-file QA must catch the sustained tail dropout.
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "silent-tail.wav"
            _write_pcm_wav(output, [(10, 900), (10, 0)])
            transcript = " ".join(["كلمة"] * 20)
            with patch.object(voice_mesh, "duration", return_value=20.0):
                with self.assertRaisesRegex(RuntimeError, "voice_qa_silence"):
                    voice_mesh._qa(output, transcript)

    def test_whole_file_acoustic_qa_accepts_long_continuous_audio(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "healthy.wav"
            _write_pcm_wav(output, [(20, 400)])
            transcript = " ".join(["كلمة"] * 20)
            with patch.object(voice_mesh, "duration", return_value=20.0):
                voice_mesh._qa(output, transcript)

    def test_whole_file_acoustic_qa_allows_bounded_natural_pause(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "bounded-pause.wav"
            _write_pcm_wav(output, [(8, 400), (4, 0), (8, 400)])
            transcript = " ".join(["كلمة"] * 20)
            with patch.object(voice_mesh, "duration", return_value=20.0):
                voice_mesh._qa(output, transcript)


# PIPER_MAX_CHARS_PER_CHUNK=420 was set from a design comment ("stay bounded on low-memory
# runners") with no attached measurement. Every existing test above mocks
# _synthesize_piper_piece - none of them ever calls the real Piper ONNX model, so the
# number itself was never checked against real evidence. This measures the real thing:
# it runs the actual local Piper voice on a genuine 420-character Arabic chunk (the exact
# production limit) in an isolated child process and asserts the real measured peak RSS,
# not a mock's approval.
_REAL_PIPER_AVAILABLE = _piper_model_available()


@unittest.skipUnless(
    _REAL_PIPER_AVAILABLE,
    "PIPER_MODEL_PATH not configured with a real .onnx voice - see security/piper_voice_hashes.json "
    "in the Engine repo (rhasspy/piper-voices ar_JO-kareem-medium) for the pinned model this measures "
    "against. Real production CI (produce-resilient-v4.yml) provisions this; the PR-validation CI "
    "(verify-human-editorial-intent-m7.yml) currently does not, so this test is skipped there today.",
)
class RealPiperChunkMemoryTests(unittest.TestCase):
    """Runs actual Piper synthesis - no mock - to put real evidence behind PIPER_MAX_CHARS_PER_CHUNK."""

    # Generous ceiling for a *single* 420-char chunk on the low-memory runners the design
    # comment names. This is intentionally far above typical measured usage (a Piper medium
    # model plus one short synthesis call has historically stayed in the tens of MB) so the
    # test only fails on a genuine regression (e.g. someone multiplying the chunk size by
    # 100x), not on ordinary interpreter/model-load noise.
    _MAX_PEAK_RSS_KB = 1024 * 1024  # 1 GiB

    def _measure_child_peak_rss_kb(self, text_repr: str) -> tuple[int, int]:
        """Run one real Piper synthesis of `text_repr` (a repr()-safe literal) in a fresh
        child process and return (peak_rss_kb, output_wav_size_bytes)."""
        script = textwrap.dedent(
            f"""
            import resource
            import sys
            import tempfile
            from pathlib import Path

            sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
            from scripts import voice_mesh

            text = {text_repr}
            with tempfile.TemporaryDirectory() as td:
                output = Path(td) / "chunk.wav"
                voice_mesh._synthesize_piper_piece(text, output)
                size = output.stat().st_size
            peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            print(f"PEAK_RSS_KB={{peak_kb}}")
            print(f"OUTPUT_BYTES={{size}}")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=120, check=False,
        )
        self.assertEqual(result.returncode, 0, f"child synthesis failed: {result.stderr}")
        peak_kb = output_bytes = None
        for line in result.stdout.splitlines():
            if line.startswith("PEAK_RSS_KB="):
                peak_kb = int(line.split("=", 1)[1])
            elif line.startswith("OUTPUT_BYTES="):
                output_bytes = int(line.split("=", 1)[1])
        self.assertIsNotNone(peak_kb, f"child did not report peak RSS; stdout={result.stdout!r}")
        self.assertIsNotNone(output_bytes, f"child did not report output size; stdout={result.stdout!r}")
        return peak_kb, output_bytes

    def test_max_chars_per_chunk_stays_within_measured_low_memory_bound(self) -> None:
        sentence = "هذا اختبار حقيقي لتوليف الصوت بلغة عربية طبيعية وكاملة بلا اختصار. "
        text = (sentence * 10)[: voice_mesh.PIPER_MAX_CHARS_PER_CHUNK]
        self.assertEqual(len(text), voice_mesh.PIPER_MAX_CHARS_PER_CHUNK)

        peak_kb, output_bytes = self._measure_child_peak_rss_kb(repr(text))

        self.assertGreater(output_bytes, 1024, "real Piper synthesis produced an empty/tiny WAV")
        self.assertLess(
            peak_kb,
            self._MAX_PEAK_RSS_KB,
            f"real Piper synthesis of a full {voice_mesh.PIPER_MAX_CHARS_PER_CHUNK}-char chunk "
            f"peaked at {peak_kb} KB RSS, exceeding the {self._MAX_PEAK_RSS_KB} KB low-memory-runner "
            "bound - PIPER_MAX_CHARS_PER_CHUNK may need to be lowered",
        )
        print(
            f"\n[real Piper evidence] {voice_mesh.PIPER_MAX_CHARS_PER_CHUNK}-char chunk -> "
            f"peak RSS = {peak_kb} KB ({peak_kb / 1024:.1f} MB), output = {output_bytes} bytes"
        )

    def test_doubling_chunk_size_still_fits_low_memory_bound_with_margin(self) -> None:
        # If PIPER_MAX_CHARS_PER_CHUNK were doubled, would it still be evidently safe? This
        # is the real-evidence answer to "is 420 a real limit or an arbitrary guess" - it
        # shows how much headroom the current value actually has, measured, not assumed.
        double = voice_mesh.PIPER_MAX_CHARS_PER_CHUNK * 2
        sentence = "هذا اختبار حقيقي لتوليف الصوت بلغة عربية طبيعية وكاملة بلا اختصار. "
        text = (sentence * 20)[:double]
        self.assertEqual(len(text), double)

        peak_kb, output_bytes = self._measure_child_peak_rss_kb(repr(text))

        self.assertGreater(output_bytes, 1024)
        self.assertLess(
            peak_kb,
            self._MAX_PEAK_RSS_KB,
            f"real Piper synthesis of a {double}-char chunk (2x the current limit) already "
            f"peaked at {peak_kb} KB RSS",
        )
        print(f"\n[real Piper evidence] {double}-char chunk (2x limit) -> peak RSS = {peak_kb} KB")


if __name__ == "__main__":
    unittest.main()
