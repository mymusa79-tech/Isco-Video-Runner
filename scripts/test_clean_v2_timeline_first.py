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
from clean_v2.visual_qa import verify_final_composition_visual_qa


class TimelineFirstDurationTests(unittest.TestCase):
    def _write_timeline(
        self,
        root: Path,
        seconds: float,
        fmt: str = "short",
        owner: str = "measured_charon_voice",
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

    def test_short_final_report_preserves_nabra_timeline_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_timeline(
                root,
                31.5,
                owner="measured_nabra_voice",
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
                "measured_nabra_voice",
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


class TimelineFirstIdentityBoundsTests(unittest.TestCase):
    def test_intro_prayer_identity_and_outro_use_measured_audio_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            narration = root / "narration-mastered.wav"
            narration.write_bytes(b"master")
            files = {
                "audio/01-chunks/01.wav": ("hook", 4.0),
                "audio/01-chunks/intro-silence.wav": ("intro_silence", 0.35),
                "audio/01-chunks/02.wav": ("prayer", 3.0),
                "audio/01-chunks/03.wav": ("channel_identity", 5.0),
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
                "audio/01-chunks/03.wav": 0,
                "audio/01-chunks/04.wav": 0,
                "audio/02.wav": 1,
                "audio/03-chunks/01.wav": 2,
                "audio/03-chunks/02.wav": 2,
                "audio/03-chunks/final-silence.wav": 2,
            }
            durations = {"narration-mastered.wav": 30.7}
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
                    return 30.7
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
            self.assertEqual((events["intro"]["start"], events["intro"]["end"]), (4.0, 4.35))
            self.assertEqual((events["prayer"]["start"], events["prayer"]["end"]), (4.35, 7.35))
            self.assertEqual(
                (events["channel_identity"]["start"], events["channel_identity"]["end"]),
                (7.35, 12.35),
            )
            self.assertEqual((events["topic"]["start"], events["topic"]["end"]), (12.35, 30.35))
            self.assertEqual((events["outro"]["start"], events["outro"]["end"]), (30.35, 30.7))
            self.assertEqual((events["final_silence"]["start"], events["final_silence"]["end"]), (30.35, 30.7))
            self.assertEqual(events["outro"]["source"], "post_payoff_terminal_silence")
            self.assertTrue(
                all(
                    row["source"].startswith("measured_")
                    for row in report["identity_events"]
                    if row["kind"] != "outro"
                )
            )

    def test_film_and_podcast_use_the_same_measured_silence_sequence(self) -> None:
        for fmt in ("film", "podcast"):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                narration = root / "narration-mastered.wav"
                narration.write_bytes(b"master")
                files = {
                    "audio/01-chunks/01.wav": ("hook", 4.0),
                    "audio/01-chunks/intro-silence.wav": ("intro_silence", 0.45),
                    "audio/01-chunks/02.wav": ("prayer", 3.0),
                    "audio/01-chunks/03.wav": ("channel_identity", 5.0),
                    "audio/01-chunks/04.wav": ("topic", 6.0),
                    "audio/02-chunks/01.wav": ("topic", 5.0),
                    "audio/02-chunks/02.wav": ("outro", 3.0),
                    "audio/02-chunks/final-silence.wav": ("final_silence", 0.45),
                }
                sections = [{"id": "s1", "chunks": []}, {"id": "s2", "chunks": []}]
                section_for = {
                    relative: (0 if "01-chunks" in relative else 1)
                    for relative in files
                }
                for relative, (role, _seconds) in files.items():
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"audio")
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
                        return 26.9
                    return files[str(path.relative_to(root))][1]

                with mock.patch("clean_v2.timeline_first.probe_duration", side_effect=duration):
                    report = build_voice_owned_timeline(
                        output_dir=root,
                        narration_path=narration,
                        fmt=fmt,
                        require_identity=True,
                    )

                events = {row["kind"]: row for row in report["identity_events"]}
                self.assertEqual((events["hook"]["start"], events["hook"]["end"]), (0.0, 4.0))
                self.assertEqual((events["intro"]["start"], events["intro"]["end"]), (4.0, 4.45))
                self.assertEqual((events["prayer"]["start"], events["prayer"]["end"]), (4.45, 7.45))
                self.assertEqual(
                    (events["channel_identity"]["start"], events["channel_identity"]["end"]),
                    (7.45, 12.45),
                )
                self.assertEqual((events["topic"]["start"], events["topic"]["end"]), (12.45, 26.45))
                self.assertEqual((events["outro"]["start"], events["outro"]["end"]), (26.45, 26.9))
                self.assertEqual(
                    (events["final_silence"]["start"], events["final_silence"]["end"]),
                    (26.45, 26.9),
                )
                self.assertEqual(events["outro"]["source"], "post_payoff_terminal_silence")

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
        self.assertNotIn("colorchannelmixer=aa=", filters)
        self.assertNotIn("alpha=1", filters)
        self.assertIn("tpad=stop_mode=clone", filters)
        self.assertIn("[0:v][intro]overlay", filters)
        self.assertIn("[v2][outro]overlay", filters)


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
                        "timeline_owner": "measured_charon_voice",
                        "identity_events": [
                            {"kind": "hook", "start": 0.0, "end": 3.0},
                            {"kind": "intro", "start": 3.0, "end": 8.0},
                            {"kind": "prayer", "start": 3.0, "end": 5.0},
                            {"kind": "channel_identity", "start": 5.0, "end": 8.0},
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
                ["hook", "intro", "prayer", "channel_identity", "topic", "outro", "final_silence"],
            )
            updated = json.loads(
                (root / "final-cut-visual-qa.json").read_text(encoding="utf-8")
            )
            self.assertTrue(updated["final_composition_review_performed"])


if __name__ == "__main__":
    unittest.main()
