from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from clean_v2.pipeline import _inspect_final_with_short_gate
from clean_v2.timeline_render import render_identity_composition
from clean_v2.timeline_first import build_voice_owned_timeline
from clean_v2.identity_sequence import identity_timing_profile
from clean_v2.visual_qa import verify_final_composition_visual_qa


class TimelineFirstDurationTests(unittest.TestCase):
    def _write_timeline(
        self,
        root: Path,
        seconds: float,
        fmt: str = "short",
        owner: str = "measured_gemini38_voice",
    ) -> None:
        (root / "timeline-first.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "contract_id": "clean-v2-timeline-first-v1",
                    "status": "pass",
                    "format": fmt,
                    "timeline_owner": owner,
                    "voice_seconds_measured": seconds,
                    "safety_maximum_seconds": 120.0 if fmt == "short" else 3600.0,
                }
            ),
            encoding="utf-8",
        )

    def test_34_48_57_second_voice_is_the_exact_final_acceptance_duration(self) -> None:
        for seconds in (34.0, 48.0, 57.0):
            with self.subTest(seconds=seconds), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self._write_timeline(root, seconds)
                report = _inspect_final_with_short_gate(
                    final_inspector=lambda _path, value=seconds: {
                        "status": "pass",
                        "duration_seconds": value,
                        "width": 1080,
                        "height": 1920,
                    },
                    output_dir=root,
                    final_path=root / "final.mp4",
                    fmt="short",
                )
                self.assertEqual(report["duration_seconds"], seconds)
                final_gate = json.loads(
                    (root / "short-duration-final.json").read_text(encoding="utf-8")
                )
                self.assertEqual(final_gate["voice_seconds"], seconds)
                self.assertEqual(final_gate["duration_delta_seconds"], 0.0)
                self.assertIsNone(final_gate["editorial_target_seconds"])

    def test_short_final_report_preserves_gemini38_timeline_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_timeline(
                root,
                31.5,
                owner="measured_gemini38_voice",
            )
            _inspect_final_with_short_gate(
                final_inspector=lambda _path: {
                    "status": "pass",
                    "duration_seconds": 31.5,
                    "width": 1080,
                    "height": 1920,
                },
                output_dir=root,
                final_path=root / "final.mp4",
                fmt="short",
            )
            final_gate = json.loads(
                (root / "short-duration-final.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                final_gate["timeline_owner"],
                "measured_gemini38_voice",
            )

    def test_same_voice_owned_duration_contract_applies_to_film(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_timeline(root, 137.25, fmt="film")
            report = _inspect_final_with_short_gate(
                final_inspector=lambda _path: {
                    "status": "pass",
                    "duration_seconds": 137.25,
                    "width": 1920,
                    "height": 1080,
                },
                output_dir=root,
                final_path=root / "final.mp4",
                fmt="film",
            )
            self.assertEqual(report["duration_seconds"], 137.25)


class LongFormFinalToleranceTests(unittest.TestCase):
    """Run 39: a 107.94 s podcast voice rendered to a 107.80 s video (0.13 %, the gap was
    closing silence) was rejected by the fixed 80 ms window."""

    def _gate(self, fmt: str, voice: float, final: float) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            TimelineFirstDurationTests()._write_timeline(root, voice, fmt=fmt)
            _inspect_final_with_short_gate(
                final_inspector=lambda _path: {
                    "status": "pass",
                    "duration_seconds": final,
                    "width": 1920,
                    "height": 1080,
                },
                output_dir=root,
                final_path=root / "final.mp4",
                fmt=fmt,
            )

    def test_podcast_accepts_the_observed_run39_duration_gap(self) -> None:
        self._gate("podcast", 107.94, 107.80)

    def test_podcast_still_rejects_a_truncated_render(self) -> None:
        from clean_v2.timeline_first import TimelineFirstError

        with self.assertRaises(TimelineFirstError):
            self._gate("podcast", 107.94, 105.0)

    def test_short_keeps_the_strict_fixed_window(self) -> None:
        from clean_v2.timeline_first import TimelineFirstError

        with self.assertRaises(TimelineFirstError):
            self._gate("short", 34.0, 34.14)


class TimelineFirstIdentityBoundsTests(unittest.TestCase):
    def test_intro_prayer_identity_and_outro_use_measured_audio_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            narration = root / "narration-mastered.wav"
            narration.write_bytes(b"master")
            files = {
                "audio/01-chunks/01.wav": ("hook", 4.0),
                "audio/01-chunks/intro-silence.wav": ("intro_silence", 0.60),
                "audio/01-chunks/02.wav": ("prayer", 3.0),
                "audio/01-chunks/post-prayer-silence.wav": ("post_prayer_silence", 0.20),
                "audio/01-chunks/03.wav": ("channel_identity", 5.0),
                "audio/01-chunks/pre-topic-silence.wav": ("pre_topic_silence", 0.35),
                "audio/01-chunks/04.wav": ("topic", 6.0),
                "audio/02.wav": ("topic", 5.0),
                "audio/03-chunks/01.wav": ("topic", 4.0),
                "audio/03-chunks/02.wav": ("outro", 3.0),
                "audio/03-chunks/final-silence.wav": ("final_silence", 0.35),
            }
            sections = [
                {"id": "s1", "chunks": []},
                {"id": "s2", "chunks": []},
                {"id": "s3", "chunks": []},
            ]
            section_for = {
                "audio/01-chunks/01.wav": 0,
                "audio/01-chunks/intro-silence.wav": 0,
                "audio/01-chunks/02.wav": 0,
                "audio/01-chunks/post-prayer-silence.wav": 0,
                "audio/01-chunks/03.wav": 0,
                "audio/01-chunks/pre-topic-silence.wav": 0,
                "audio/01-chunks/04.wav": 0,
                "audio/02.wav": 1,
                "audio/03-chunks/01.wav": 2,
                "audio/03-chunks/02.wav": 2,
                "audio/03-chunks/final-silence.wav": 2,
            }
            durations = {"narration-mastered.wav": 31.5}
            for relative, (role, seconds) in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"audio")
                durations[path.name if "/" not in relative else relative] = seconds
                sections[section_for[relative]]["chunks"].append(
                    {"file": relative, "role": role}
                )
            (root / "voice-sections.json").write_text(
                json.dumps({"status": "pass", "sections": sections}),
                encoding="utf-8",
            )

            def duration(path: Path) -> float:
                path = Path(path)
                if path.name == "narration-mastered.wav":
                    return 31.5
                return files[str(path.relative_to(root))][1]

            with mock.patch("clean_v2.timeline_first.probe_duration", side_effect=duration):
                report = build_voice_owned_timeline(
                    output_dir=root,
                    narration_path=narration,
                    fmt="short",
                    require_identity=True,
                )

            events = {row["kind"]: row for row in report["identity_events"]}
            self.assertEqual((events["hook"]["start"], events["hook"]["end"]), (0.0, 4.0))
            self.assertEqual((events["intro"]["start"], events["intro"]["end"]), (4.0, 4.6))
            self.assertEqual((events["prayer"]["start"], events["prayer"]["end"]), (4.6, 7.6))
            self.assertEqual(
                (events["post_prayer_silence"]["start"], events["post_prayer_silence"]["end"]),
                (7.6, 7.8),
            )
            self.assertEqual(
                (events["channel_identity"]["start"], events["channel_identity"]["end"]),
                (7.8, 12.8),
            )
            self.assertEqual(
                (events["pre_topic_silence"]["start"], events["pre_topic_silence"]["end"]),
                (12.8, 13.15),
            )
            self.assertEqual((events["topic"]["start"], events["topic"]["end"]), (13.15, 31.15))
            self.assertEqual((events["outro"]["start"], events["outro"]["end"]), (31.15, 31.5))
            self.assertEqual((events["final_silence"]["start"], events["final_silence"]["end"]), (31.15, 31.5))
            self.assertEqual(events["outro"]["source"], "post_payoff_terminal_silence")
            self.assertTrue(
                all(
                    row["source"].startswith("measured_")
                    for row in report["identity_events"]
                    if row["kind"] != "outro"
                )
            )

    def test_podcast_sequence_starts_music_window_with_first_B_answer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            narration = root / "narration-mastered.wav"
            narration.write_bytes(b"master")
            files = {
                "audio/01-chunks/01.wav": ("hook", 4.0),
                "audio/01-chunks/post-hook-silence.wav": ("post_hook_silence", 0.75),
                "audio/01-chunks/intro-silence.wav": ("intro_silence", 6.0),
                "audio/01-chunks/02.wav": ("prayer", 3.0),
                "audio/01-chunks/post-prayer-silence.wav": ("post_prayer_silence", 0.65),
                "audio/01-chunks/03.wav": ("topic", 6.0),
                "audio/02-chunks/01.wav": ("topic", 5.0),
                "audio/02-chunks/02.wav": ("outro", 3.0),
                "audio/02-chunks/final-silence.wav": ("final_silence", 6.5),
            }
            sections = [{"id": "s1", "chunks": []}, {"id": "s2", "chunks": []}]
            for relative, (role, _seconds) in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"audio")
                section_index = 0 if "01-chunks" in relative else 1
                sections[section_index]["chunks"].append({"file": relative, "role": role})
            (root / "voice-sections.json").write_text(
                json.dumps({"status": "pass", "sections": sections}),
                encoding="utf-8",
            )

            def duration(path: Path) -> float:
                path = Path(path)
                if path.name == "narration-mastered.wav":
                    return 34.9
                return files[str(path.relative_to(root))][1]

            with mock.patch("clean_v2.timeline_first.probe_duration", side_effect=duration):
                report = build_voice_owned_timeline(
                    output_dir=root,
                    narration_path=narration,
                    fmt="podcast",
                    require_identity=True,
                )

            events = {row["kind"]: row for row in report["identity_events"]}
            self.assertEqual((events["hook"]["start"], events["hook"]["end"]), (0.0, 4.0))
            self.assertEqual(
                (events["post_hook_silence"]["start"], events["post_hook_silence"]["end"]),
                (4.0, 4.75),
            )
            self.assertEqual((events["intro"]["start"], events["intro"]["end"]), (4.75, 10.75))
            self.assertEqual((events["prayer"]["start"], events["prayer"]["end"]), (10.75, 13.75))
            self.assertEqual(
                (events["post_prayer_silence"]["start"], events["post_prayer_silence"]["end"]),
                (13.75, 14.4),
            )
            self.assertNotIn("channel_identity", events)
            self.assertNotIn("pre_topic_silence", events)
            self.assertEqual((events["topic"]["start"], events["topic"]["end"]), (14.4, 28.4))
            self.assertEqual((events["outro"]["start"], events["outro"]["end"]), (28.4, 34.9))
            self.assertEqual(
                (events["final_silence"]["start"], events["final_silence"]["end"]),
                (28.4, 34.9),
            )

    def test_film_keeps_existing_measured_identity_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            narration = root / "narration-mastered.wav"
            narration.write_bytes(b"master")
            files = {
                "audio/01-chunks/01.wav": ("hook", 4.0),
                "audio/01-chunks/intro-silence.wav": ("intro_silence", 0.70),
                "audio/01-chunks/02.wav": ("prayer", 3.0),
                "audio/01-chunks/post-prayer-silence.wav": ("post_prayer_silence", 0.30),
                "audio/01-chunks/03.wav": ("channel_identity", 5.0),
                "audio/01-chunks/pre-topic-silence.wav": ("pre_topic_silence", 0.45),
                "audio/01-chunks/04.wav": ("topic", 6.0),
                "audio/02-chunks/01.wav": ("topic", 5.0),
                "audio/02-chunks/02.wav": ("outro", 3.0),
                "audio/02-chunks/final-silence.wav": ("final_silence", 0.45),
            }
            sections = [{"id": "s1", "chunks": []}, {"id": "s2", "chunks": []}]
            for relative, (role, _seconds) in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"audio")
                section_index = 0 if "01-chunks" in relative else 1
                sections[section_index]["chunks"].append({"file": relative, "role": role})
            (root / "voice-sections.json").write_text(
                json.dumps({"status": "pass", "sections": sections}),
                encoding="utf-8",
            )

            def duration(path: Path) -> float:
                path = Path(path)
                if path.name == "narration-mastered.wav":
                    return 27.9
                return files[str(path.relative_to(root))][1]

            with mock.patch("clean_v2.timeline_first.probe_duration", side_effect=duration):
                report = build_voice_owned_timeline(
                    output_dir=root,
                    narration_path=narration,
                    fmt="film",
                    require_identity=True,
                )

            events = {row["kind"]: row for row in report["identity_events"]}
            self.assertEqual((events["hook"]["start"], events["hook"]["end"]), (0.0, 4.0))
            self.assertEqual((events["intro"]["start"], events["intro"]["end"]), (4.0, 4.7))
            self.assertEqual((events["prayer"]["start"], events["prayer"]["end"]), (4.7, 7.7))
            self.assertEqual(
                (events["channel_identity"]["start"], events["channel_identity"]["end"]),
                (8.0, 13.0),
            )
            self.assertEqual((events["topic"]["start"], events["topic"]["end"]), (13.45, 27.45))


    def test_identity_animation_preserves_the_story_world_beneath_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = {}
            for name in ("intro", "prayer", "outro"):
                path = root / f"{name}.asset"
                path.write_bytes(b"A" * 2048)
                assets[name] = path
            timeline = {
                "voice_seconds_measured": 12.0,
                "identity_events": [
                    {"kind": "intro", "start": 2.0, "end": 3.0},
                    {"kind": "prayer", "start": 3.0, "end": 4.5},
                    {"kind": "outro", "start": 10.0, "end": 11.25},
                    {"kind": "final_silence", "start": 11.25, "end": 12.0},
                ],
            }
            with mock.patch(
                "clean_v2.timeline_render.identity_asset_paths",
                return_value=assets,
            ), mock.patch("clean_v2.timeline_render.subprocess.run") as run:
                render_identity_composition(
                    root / "source.mp4",
                    root / "destination.mp4",
                    fmt="short",
                    timeline=timeline,
                )

        command = run.call_args.args[0]
        filters = command[command.index("-filter_complex") + 1]
        self.assertNotIn("-stream_loop", command)
        self.assertNotIn("colorchannelmixer=aa=", filters)
        self.assertNotIn("alpha=1", filters)
        self.assertIn("tpad=stop_mode=clone", filters)
        self.assertNotIn("ass='", filters)
        self.assertIn("[v1][prayer]overlay=0:0", filters)
        self.assertIn("between(t,3.000,4.500)", filters)
        self.assertIn("[0:v][intro]overlay", filters)
        self.assertIn("[v2][outro]overlay", filters)
        self.assertIn(str(assets["prayer"]), command)

    def test_podcast_identity_pieces_stay_opaque_over_the_same_story_world(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = {}
            for name in ("intro", "prayer", "outro"):
                path = root / f"{name}.asset"
                path.write_bytes(b"A" * 2048)
                assets[name] = path
            timeline = {
                "voice_seconds_measured": 15.0,
                "identity_events": [
                    {"kind": "intro", "start": 2.0, "end": 8.0},
                    {"kind": "prayer", "start": 8.0, "end": 10.0},
                    {"kind": "outro", "start": 10.0, "end": 15.0},
                    {"kind": "final_silence", "start": 10.0, "end": 15.0},
                ],
            }
            with mock.patch(
                "clean_v2.timeline_render.identity_asset_paths",
                return_value=assets,
            ), mock.patch("clean_v2.timeline_render.subprocess.run") as run:
                render_identity_composition(
                    root / "source.mp4",
                    root / "destination.mp4",
                    fmt="podcast",
                    timeline=timeline,
                )
        command = run.call_args.args[0]
        filters = command[command.index("-filter_complex") + 1]
        self.assertNotIn("alpha=1", filters)
        self.assertNotIn("fade=t=out:", filters)
        self.assertIn("[0:v][intro]overlay", filters)
        self.assertIn("[v1][prayer]overlay", filters)
        self.assertIn("[v2][outro]overlay", filters)
        self.assertIn("[1:a]atrim", filters)
        self.assertIn("[3:a]atrim", filters)
        self.assertIn("[0:a][aintro][aoutro]amix", filters)

    def test_prayer_uses_existing_image_only_without_duplicate_caption_layer(self) -> None:
        source = Path("clean_v2/timeline_render.py").read_text(encoding="utf-8")
        self.assertIn('assets["prayer"]', source)
        self.assertIn("[v1][prayer]overlay", source)
        self.assertNotIn("_prayer_ass", source)
        self.assertNotIn("PRAYER_SENTENCE", source)

    def test_terminal_outro_breathing_window_is_format_specific(self) -> None:
        self.assertEqual(identity_timing_profile("short")["post_prayer_silence_seconds"], 0.35)
        self.assertEqual(identity_timing_profile("film")["post_prayer_silence_seconds"], 0.45)
        self.assertEqual(identity_timing_profile("podcast")["post_prayer_silence_seconds"], 0.65)
        self.assertEqual(identity_timing_profile("podcast")["intro_silence_seconds"], 6.00)
        self.assertEqual(identity_timing_profile("podcast")["pre_topic_silence_seconds"], 0.00)
        self.assertEqual(identity_timing_profile("short")["final_silence_seconds"], 1.25)
        self.assertEqual(identity_timing_profile("film")["final_silence_seconds"], 3.50)
        self.assertEqual(identity_timing_profile("podcast")["final_silence_seconds"], 6.50)


class FinalCompositionVisualQATests(unittest.TestCase):
    def test_visual_qa_reads_final_composition_with_identity_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            final_path = root / "final.mp4"
            final_path.write_bytes(b"COMPOSED-FINAL-WITH-IDENTITY")
            (root / "timeline-first.json").write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "contract_id": "clean-v2-timeline-first-v1",
                        "timeline_owner": "measured_gemini38_voice",
                        "identity_events": [
                            {"kind": "hook", "start": 0.0, "end": 3.0},
                            {"kind": "intro", "start": 3.0, "end": 8.0},
                            {"kind": "prayer", "start": 3.0, "end": 5.0},
                            {"kind": "post_prayer_silence", "start": 5.0, "end": 5.35},
                            {"kind": "channel_identity", "start": 5.35, "end": 7.65},
                            {"kind": "pre_topic_silence", "start": 7.65, "end": 8.0},
                            {"kind": "topic", "start": 8.0, "end": 31.0},
                            {"kind": "outro", "start": 31.0, "end": 33.25},
                            {"kind": "final_silence", "start": 33.25, "end": 34.0},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (root / "final-cut-visual-qa.json").write_text(
                json.dumps({"status": "pass"}),
                encoding="utf-8",
            )
            seen: list[Path] = []

            def evidence(*, final_path, **_kwargs):
                seen.append(Path(final_path))
                return {
                    "mode": "test_final_composition",
                    "prompt_hash": "prompt",
                    "frame_sha256": ["frame"],
                }

            with mock.patch(
                "clean_v2.visual_qa._build_final_composition_evidence",
                side_effect=evidence,
            ):
                result = verify_final_composition_visual_qa(
                    output_dir=root,
                    final_path=final_path,
                    script={"sections": [{"id": "s1", "narration": "نص"}]},
                )

            self.assertEqual(seen, [final_path])
            self.assertEqual(result["evidence_mode"], "test_final_composition")
            self.assertEqual(result["source_media"], "final.mp4")
            self.assertEqual(
                result["identity_event_kinds"],
                ["hook", "intro", "prayer", "post_prayer_silence", "channel_identity", "pre_topic_silence", "topic", "outro", "final_silence"],
            )
            updated = json.loads(
                (root / "final-cut-visual-qa.json").read_text(encoding="utf-8")
            )
            self.assertTrue(updated["final_composition_review_performed"])


if __name__ == "__main__":
    unittest.main()
