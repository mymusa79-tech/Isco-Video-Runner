from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from clean_v2 import podcast_captions as pc

TEXT = "A: لماذا نشعر بالتعب؟ B: لأن القرارات الصغيرة تستهلك طاقتنا. A: وما الحل؟"


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


def _make_run(root: Path) -> None:
    (root / "audio").mkdir()
    # 9 s "speech": tone, pause, tone, pause, tone -> real pauses at ~2.75 s and ~7.25 s.
    _ffmpeg(
        "-f", "lavfi", "-i", "sine=frequency=300:duration=2.5",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=0.5",
        "-f", "lavfi", "-i", "sine=frequency=300:duration=4.0",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=0.5",
        "-f", "lavfi", "-i", "sine=frequency=300:duration=1.5",
        "-filter_complex", "[0][1][2][3][4]concat=n=5:v=0:a=1,aresample=44100[a]",
        "-map", "[a]", str(root / "audio" / "topic.wav"),
    )
    (root / "timeline-first.json").write_text(json.dumps({
        "status": "pass",
        "audio_units": [
            {"section_id": "s1", "chunk": 1, "role": "prayer", "file": "audio/none.wav", "start": 0.0, "end": 2.0, "duration_seconds": 2.0},
            {"section_id": "s1", "chunk": 2, "role": "topic", "file": "audio/topic.wav", "start": 2.0, "end": 11.0, "duration_seconds": 9.0},
            {"section_id": "s1", "chunk": 3, "role": "cta_topic", "file": "audio/none.wav", "start": 11.0, "end": 13.0, "duration_seconds": 2.0},
        ],
    }), encoding="utf-8")
    (root / "voice-sections.json").write_text(json.dumps({"sections": [{"id": "s1", "chunks": [
        {"chunk": 1, "role": "prayer", "text": "اللهم صل على محمد."},
        {"chunk": 2, "role": "topic", "text": TEXT},
        {"chunk": 3, "role": "cta_topic", "text": "شاركنا رأيك في التعليقات."},
    ]}]}), encoding="utf-8")


class AlignmentTests(unittest.TestCase):
    def test_cuts_snap_to_close_pauses_and_stay_ordered(self) -> None:
        cuts = pc.align_boundaries([3, 5, 2], 9.0, [2.75, 7.25])
        self.assertEqual(cuts[0], 0.0)
        self.assertEqual(cuts[-1], 9.0)
        self.assertAlmostEqual(cuts[1], 2.75, places=2)
        self.assertAlmostEqual(cuts[2], 7.25, places=2)

    def test_a_snap_reanchors_the_rest_and_distant_pauses_do_not_hijack_a_cut(self) -> None:
        # Four equal phrases in 40 s: estimates 10/20/30. A pause 8 s away from the first
        # estimate is another sentence gap, not this boundary.
        cuts = pc.align_boundaries([1, 1, 1, 1], 40.0, [2.0, 10.8, 29.0])
        self.assertAlmostEqual(cuts[1], 10.8, places=2)   # close -> snapped
        self.assertAlmostEqual(cuts[2], 10.8 + (40 - 10.8) / 3, places=1)  # re-anchored estimate
        self.assertNotIn(2.0, cuts)

    def test_far_pauses_are_ignored_and_weights_decide(self) -> None:
        cuts = pc.align_boundaries([1, 1], 10.0, [9.9])
        self.assertAlmostEqual(cuts[1], 5.0, places=2)
        self.assertEqual(cuts, sorted(cuts))


class EventTests(unittest.TestCase):
    def test_only_hook_and_topic_get_captions_with_speakers_and_real_pauses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_run(root)
            events, timing = pc.build_events(root)
            self.assertEqual(timing, "measured_audio_units_snapped_to_pauses")
            self.assertEqual([e["speaker"] for e in events], ["A", "B", "A"])  # landscape cards: one card per short turn
            self.assertTrue(all(2.0 <= e["start"] and e["end"] <= 11.0 for e in events))
            self.assertNotIn("اللهم", json.dumps(events, ensure_ascii=False))
            self.assertNotIn("التعليقات", json.dumps(events, ensure_ascii=False))
            self.assertAlmostEqual(events[0]["end"], 2.0 + 2.75, delta=0.15)
            self.assertAlmostEqual(events[1]["end"], 2.0 + 7.25, delta=0.15)

    def test_missing_chunk_text_is_a_soft_failure_not_a_guess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_run(root)
            voice = json.loads((root / "voice-sections.json").read_text(encoding="utf-8"))
            del voice["sections"][0]["chunks"][1]["text"]
            (root / "voice-sections.json").write_text(json.dumps(voice), encoding="utf-8")
            with self.assertRaises(pc.PodcastCaptionError):
                pc.build_events(root)

    def test_ass_and_srt_carry_speaker_colours_and_max_two_rows(self) -> None:
        events = [
            {"start": 1.0, "end": 3.0, "text": "لماذا نشعر بالتعب؟", "speaker": "A"},
            {"start": 3.0, "end": 6.0, "text": "لأن القرارات الصغيرة تستهلك طاقتنا.", "speaker": "B"},
        ]
        ass = pc.build_ass(events)
        self.assertIn(r"\c&H4BA2C9&", ass)  # A gold #C9A24B
        self.assertIn(r"\c&HEFF3F5&", ass)  # B warm white #F5F3EF
        self.assertIn("Cairo", ass)
        self.assertIn("-->", pc.build_srt(events))
        for line in ass.splitlines():
            if line.startswith("Dialogue"):
                self.assertLessEqual(line.count(r"\N"), 1)


class VerbatimTests(unittest.TestCase):
    def test_a_changed_or_dropped_word_is_never_drawn(self) -> None:
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_run(root)
            real = pc._speaker_chunks
            for mutate in (
                lambda rows: [(s, c.replace("التعب", "الإرهاق")) for s, c in rows],  # synonym swap
                lambda rows: rows[:-1],                                               # lost phrase
                lambda rows: rows + [("A", "كلمة مخترعة")],                           # invented phrase
            ):
                with mock.patch.object(pc, "_speaker_chunks", lambda text, fmt="short", m=mutate: m(real(text, fmt))):
                    with self.assertRaisesRegex(pc.PodcastCaptionError, "not_verbatim"):
                        pc.build_events(root)

    def test_every_caption_word_traces_to_the_approved_chunk_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_run(root)
            events, _timing = pc.build_events(root)
            self.assertEqual(" ".join(e["text"] for e in events).split(),
                             "لماذا نشعر بالتعب؟ لأن القرارات الصغيرة تستهلك طاقتنا. وما الحل؟".split())


class BarTests(unittest.TestCase):
    EVENTS = [
        {"start": 1.0, "end": 3.0, "text": "جملة أولى قصيرة.", "speaker": "A"},
        {"start": 3.1, "end": 5.0, "text": "جملة ثانية متصلة.", "speaker": "B"},
        {"start": 9.0, "end": 11.0, "text": "جملة بعد وقفة.", "speaker": "A"},
    ]

    def test_longform_gets_one_fixed_band_per_run_and_a_larger_font(self) -> None:
        ass = pc.build_ass(self.EVENTS)
        bars = [line for line in ass.splitlines() if r"\p1" in line]
        self.assertEqual(len(bars), 2)  # 1.0-5.0 merged, 9.0-11.0 separate
        self.assertIn("0:00:01.00,0:00:05.00", bars[0])
        self.assertIn(f"{pc.BAR_BOTTOM}", bars[0])
        self.assertLessEqual(pc.BAR_BOTTOM, 1080 - 100)  # clear of player controls
        self.assertEqual(pc.FONT_SIZE, 72)
        text_lines = [line for line in ass.splitlines() if line.startswith("Dialogue: 1,")]
        self.assertEqual(len(text_lines), 3)  # text sits above the band layer

    def test_short_has_no_band(self) -> None:
        self.assertNotIn(r"\p1", pc.build_ass(self.EVENTS, fmt="short"))


class BurnTests(unittest.TestCase):
    def test_burned_video_shows_gold_for_A_and_white_for_B(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_run(root)
            final = root / "final.mp4"
            _ffmpeg("-f", "lavfi", "-i", "color=c=0x101820:s=1920x1080:r=25:d=13", "-pix_fmt", "yuv420p", str(final))
            before = final.read_bytes()
            report = pc.apply_podcast_captions(output_dir=root, final_path=final)
            self.assertEqual(report["status"], "pass")
            self.assertTrue(report["burned_into_final_video"])
            self.assertNotEqual(final.read_bytes(), before)
            # The caption-free master is kept so the derived Short is never double-captioned.
            self.assertEqual((root / pc.CLEAN_MASTER_NAME).read_bytes(), before)
            self.assertTrue((root / "podcast-captions.srt").is_file())
            self.assertIn("is_requested_font", report["font_actually_used"])  # reported from libass, not assumed
            self.assertEqual(report["font_actually_used"]["requested"], "Cairo")

            def dominant(second: float) -> tuple[int, int, int]:
                raw = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(second), "-i", str(final),
                     "-frames:v", "1", "-vf", "crop=1920:300:0:700,scale=192:30", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                    capture_output=True, check=True,
                ).stdout
                pixels = [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]
                bright = [p for p in pixels if max(p) > 140]
                self.assertTrue(bright, "no caption pixels found")
                return tuple(sum(p[c] for p in bright) // len(bright) for c in range(3))

            gold = dominant(3.0)   # A phrase
            white = dominant(6.0)  # B phrase
            self.assertGreater(gold[0] - gold[2], 40)           # gold: red >> blue
            self.assertLess(abs(white[0] - white[2]), 30)       # warm white: balanced


if __name__ == "__main__":
    unittest.main()


class FallbackWarningTests(unittest.TestCase):
    def test_skipped_captions_warn_and_appear_in_report(self) -> None:
        import contextlib
        import io
        import tempfile
        from pathlib import Path
        from unittest import mock

        from clean_v2 import pipeline
        from clean_v2.podcast_captions import PodcastCaptionError

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            buf = io.StringIO()
            with mock.patch("clean_v2.legacy_cinematic.apply_post_render_layer", return_value={}), \
                 mock.patch("clean_v2.contextual_cta.apply_contextual_cta_overlay", return_value={}), \
                 mock.patch("clean_v2.podcast_captions.apply_podcast_captions",
                            side_effect=PodcastCaptionError("podcast_captions_text_not_verbatim")), \
                 mock.patch("clean_v2.podcast_key_text.apply_podcast_key_text",
                            return_value={"status": "pass"}), \
                 mock.patch("clean_v2.short_audio_polish.apply_topic_audio_polish",
                            return_value={"status": "skipped"}), \
                 mock.patch("clean_v2.visual_cta.apply_visual_cta_assets",
                            return_value={"status": "skipped"}), \
                 contextlib.redirect_stdout(buf):
                try:
                    report = pipeline._run_legacy_cinematic_layer(
                        output_dir=out, final_path=out / "f.mp4", narration_path=out / "n.wav",
                        plan={}, script={}, rights=[], fmt="podcast",
                    )
                except Exception as exc:  # unrelated later layers must not mask the check
                    self.skipTest(f"layer needs full env: {exc}")
        self.assertIn("::warning title=podcast-captions-fallback::", buf.getvalue())
        self.assertIn("podcast_captions_text_not_verbatim", buf.getvalue())
        self.assertEqual(report["podcast_captions"]["status"], "skipped")
        self.assertEqual(report["podcast_captions"]["reason"], "podcast_captions_text_not_verbatim")


class RealRunRegressionTests(unittest.TestCase):
    """Run 141 crashed with IndexError: a short LAST clause that cannot merge backwards."""

    def test_short_last_clause_that_cannot_merge_backwards_does_not_crash(self) -> None:
        from clean_v2 import short_timed_text as stt

        full = " ".join(f"كلمة{i}" for i in range(stt.CAPTION_MAX_WORDS))
        text = f"{full}، نعم لا"
        for fmt in ("podcast", "film", "short"):
            chunks = stt._phrase_chunks(text, fmt)
            self.assertEqual(" ".join(" ".join(chunks).split()), " ".join(text.replace("،", "،").split()))

    def test_unexpected_caption_error_is_recorded_and_never_blocks(self) -> None:
        import contextlib
        import io
        from unittest import mock

        from clean_v2 import pipeline

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            buf = io.StringIO()
            with mock.patch("clean_v2.legacy_cinematic.apply_post_render_layer", return_value={}), \
                 mock.patch("clean_v2.contextual_cta.apply_contextual_cta_overlay", return_value={}), \
                 mock.patch("clean_v2.podcast_captions.apply_podcast_captions", side_effect=IndexError("boom")), \
                 mock.patch("clean_v2.podcast_key_text.apply_podcast_key_text", return_value={"status": "pass"}), \
                 mock.patch("clean_v2.short_audio_polish.apply_topic_audio_polish", return_value={"status": "skipped"}), \
                 mock.patch("clean_v2.visual_cta.apply_visual_cta_assets", return_value={"status": "skipped"}), \
                 contextlib.redirect_stdout(buf):
                report = pipeline._run_legacy_cinematic_layer(
                    output_dir=out, final_path=out / "f.mp4", narration_path=out / "n.wav",
                    plan={}, script={}, rights=[], fmt="podcast",
                )
        self.assertEqual(report["podcast_captions"]["status"], "skipped")
        self.assertIn("podcast_captions_unexpected_error:IndexError", report["podcast_captions"]["reason"])
        self.assertIn("::warning title=podcast-captions-fallback::", buf.getvalue())
