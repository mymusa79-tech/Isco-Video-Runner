from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from clean_v2 import media as media_module
from clean_v2 import short_audio_polish as audio_module
from clean_v2.pipeline import _planning_prompt
from clean_v2.visual_story import validate_visual_story


def _plan() -> dict:
    return {
        "promise": "وعد واضح",
        "sections": [
            {
                "id": "s1",
                "purpose": "يفهم المشاهد التوتر",
                "visual_query_en": "worker sorting crowded task board",
            },
            {
                "id": "s2",
                "purpose": "يرى المشاهد النتيجة",
                "visual_query_en": "worker closing completed task board",
            },
        ],
    }


def _story() -> dict:
    return {
        "visual_world": "grounded real workplace",
        "story_arc": {
            "beginning": "friction",
            "transformation": "choice",
            "arrival": "completion",
        },
        "retention_thread": {
            "hook_tension": "تراكم العمل",
            "payoff_answer": "وضوح الأولوية",
            "visual_motif": "لوحة المهام",
        },
        "beats": [
            {
                "id": "b1",
                "section_id": "s1",
                "viewer_intent": "يرى التراكم",
                "meaning_target": "visible overload",
                "shot_intent": "worker sorting crowded task board",
                "role": "hook",
                "stock_query_en": "worker sorting crowded task board",
                "display_text_ar": "تراكم صامت",
                "source_preference": "stock_motion",
            },
            {
                "id": "b2",
                "section_id": "s2",
                "viewer_intent": "يرى الاكتمال",
                "meaning_target": "visible completion",
                "shot_intent": "worker closing completed task board",
                "role": "payoff",
                "stock_query_en": "worker closing completed task board",
                "display_text_ar": "وضوح أخير",
                "source_preference": "stock_motion",
            },
        ],
    }


class HumanEditorialRhythmTests(unittest.TestCase):
    def test_old_visual_story_receives_deterministic_signal_defaults(self) -> None:
        validated = validate_visual_story(_story(), _plan())
        hook, payoff = validated["beats"]
        self.assertEqual(hook["hold_reason"], "hook_progression")
        self.assertEqual(hook["pause_intent"], "micro")
        self.assertEqual(hook["audio_energy"], "steady")
        self.assertEqual(hook["shot_role"], "action")
        self.assertTrue(hook["environment_family"])
        self.assertEqual(payoff["hold_reason"], "payoff_landing")
        self.assertEqual(payoff["pause_intent"], "ending")
        self.assertEqual(payoff["audio_energy"], "resolve")
        self.assertEqual(payoff["shot_role"], "payoff")
        self.assertTrue(payoff["environment_family"])

    def test_explicit_signals_are_preserved_and_invalid_signal_fails_closed(self) -> None:
        story = _story()
        story["beats"][0].update(
            {
                "hold_reason": "idea_continues",
                "pause_intent": "emphasis",
                "audio_energy": "quiet",
                "shot_role": "detail",
                "environment_family": "workplace",
            }
        )
        validated = validate_visual_story(story, _plan())
        self.assertEqual(validated["beats"][0]["hold_reason"], "idea_continues")
        self.assertEqual(validated["beats"][0]["pause_intent"], "emphasis")
        self.assertEqual(validated["beats"][0]["audio_energy"], "quiet")
        self.assertEqual(validated["beats"][0]["shot_role"], "detail")
        self.assertEqual(validated["beats"][0]["environment_family"], "workplace")

        story = _story()
        story["beats"][0]["hold_reason"] = "random_longer"
        with self.assertRaisesRegex(ValueError, "invalid hold_reason"):
            validate_visual_story(story, _plan())

        story = _story()
        story["beats"][0]["shot_role"] = "random_camera_move"
        with self.assertRaisesRegex(ValueError, "invalid shot_role"):
            validate_visual_story(story, _plan())

    def test_planning_prompt_exposes_only_meaning_led_signals(self) -> None:
        brief = {
            "approved_by_user": True,
            "approved_topic": "ضغط العمل",
            "format": "short",
            "language": "ar",
            "audience": "Arabic-speaking adults",
            "editorial_intent": "شرح عملي",
            "research_pack": [],
            "hard_constraints": [],
        }
        prompt = _planning_prompt(brief)
        self.assertIn("hold_reason", prompt)
        self.assertIn("pause_intent", prompt)
        self.assertIn("audio_energy", prompt)
        self.assertIn("shot_role", prompt)
        self.assertIn("environment_family", prompt)
        self.assertIn("EDITOR CONTRACT — SHORT", prompt)
        self.assertIn("zero extra provider calls", prompt)
        self.assertIn("never random variation", prompt)
        self.assertIn("never inserts silence", prompt)

    def test_editor_contract_is_format_specific_without_new_stage(self) -> None:
        base = {
            "approved_by_user": True,
            "approved_topic": "ضغط العمل",
            "language": "ar",
            "audience": "Arabic-speaking adults",
            "editorial_intent": "شرح عملي",
            "research_pack": [],
            "hard_constraints": [],
        }
        film_prompt = _planning_prompt({**base, "format": "film"})
        podcast_prompt = _planning_prompt({**base, "format": "podcast"})
        self.assertIn("EDITOR CONTRACT — FILM", film_prompt)
        self.assertNotIn("EDITOR CONTRACT — PODCAST", film_prompt)
        self.assertIn("EDITOR CONTRACT — PODCAST", podcast_prompt)
        self.assertIn("an A question never forces a cut", podcast_prompt)

    def test_hold_reason_redistributes_only_existing_section_time(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "rights-manifest.json").write_text(
                json.dumps(
                    {
                        "assets": [
                            {
                                "local_file": "one.mp4",
                                "section_id": "s1",
                                "beat_id": "b1",
                                "hold_reason": "idea_continues",
                            },
                            {
                                "local_file": "two.mp4",
                                "section_id": "s1",
                                "beat_id": "b2",
                                "hold_reason": "idea_changes",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (root / "timeline-first.json").write_text(
                json.dumps(
                    {
                        "section_events": [
                            {"section_id": "s1", "start": 0.0, "end": 10.0}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            durations = media_module._section_slot_durations(
                root,
                [root / "one.mp4", root / "two.mp4"],
                10.0,
                pad=0.0,
            )
            self.assertAlmostEqual(sum(durations), 10.0, places=6)
            self.assertGreater(durations[0], durations[1])

    def test_audio_energy_and_pause_cues_map_inside_voice_owned_section(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "rights-manifest.json").write_text(
                json.dumps(
                    {
                        "assets": [
                            {
                                "local_file": "one.mp4",
                                "section_id": "s1",
                                "beat_id": "b1",
                                "hold_reason": "idea_continues",
                                "pause_intent": "emphasis",
                                "audio_energy": "quiet",
                            },
                            {
                                "local_file": "two.mp4",
                                "section_id": "s1",
                                "beat_id": "b2",
                                "hold_reason": "idea_changes",
                                "pause_intent": "transition",
                                "audio_energy": "lift",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (root / "timeline-first.json").write_text(
                json.dumps(
                    {
                        "section_events": [
                            {"section_id": "s1", "start": 0.0, "end": 10.0}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            windows = audio_module._editorial_audio_windows(
                root,
                topic_start=0.0,
                topic_end=10.0,
            )
            self.assertEqual(len(windows), 2)
            self.assertEqual(windows[0]["audio_energy"], "quiet")
            self.assertEqual(windows[0]["pause_intent"], "emphasis")
            self.assertEqual(windows[1]["audio_energy"], "lift")
            self.assertEqual(windows[1]["pause_intent"], "transition")
            self.assertAlmostEqual(windows[0]["start"], 0.0, places=3)
            self.assertAlmostEqual(windows[-1]["end"], 10.0, places=3)
            self.assertGreater(windows[0]["end"], 5.0)


if __name__ == "__main__":
    unittest.main()
