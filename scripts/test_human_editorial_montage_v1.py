from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import human_editorial_montage_v1 as montage
from scripts import orchestration_shorts_port as short_port
from scripts import short_voice_owned_timeline as voice_owner


class HumanEditorialMontageV1Tests(unittest.TestCase):
    def _events(self) -> list[dict[str, object]]:
        return [
            {"start": 0.0, "end": 3.0, "text": "Hook exact", "role": "hook"},
            {"start": 3.0, "end": 6.0, "text": "Beat exact", "role": "beat"},
            {"start": 6.0, "end": 9.0, "text": "Turn exact", "role": "beat"},
            {"start": 9.0, "end": 12.0, "text": "Payoff exact", "role": "payoff"},
        ]

    def test_human_cut_timing_preserves_words_roles_total_and_hook_cap(self) -> None:
        source = self._events()
        humanized, evidence = montage.humanize_event_windows(source, "why_reframe")

        self.assertEqual([row["text"] for row in humanized], [row["text"] for row in source])
        self.assertEqual([row["role"] for row in humanized], [row["role"] for row in source])
        self.assertEqual(humanized[0]["start"], source[0]["start"])
        self.assertEqual(humanized[-1]["end"], source[-1]["end"])
        self.assertLess(float(humanized[0]["end"]), float(source[0]["end"]))
        self.assertLessEqual(float(humanized[0]["end"]), 3.0)
        for left, right in zip(humanized, humanized[1:]):
            self.assertAlmostEqual(float(left["end"]), float(right["start"]), places=3)
        self.assertTrue(all(abs(int(row["effective_offset_ms"])) <= 220 for row in evidence["boundaries"]))
        self.assertFalse(evidence["speech_speed_changed"])
        self.assertFalse(evidence["words_changed"])
        self.assertEqual(evidence["extra_ai_calls"], 0)

    def test_template_rhythm_maps_are_deliberately_not_identical(self) -> None:
        why, _ = montage.humanize_event_windows(self._events(), "why_reframe")
        story, _ = montage.humanize_event_windows(self._events(), "micro_story")
        quote, _ = montage.humanize_event_windows(self._events(), "quote_reflection")
        self.assertNotEqual([row["end"] for row in why[:-1]], [row["end"] for row in story[:-1]])
        self.assertNotEqual([row["end"] for row in story[:-1]], [row["end"] for row in quote[:-1]])

    def test_selective_text_choreography_delays_only_one_non_hook_reveal(self) -> None:
        source = self._events()
        rendered, evidence = montage.choreograph_text_events(source, "inner_dialogue")
        changed_starts = [
            index
            for index, (before, after) in enumerate(zip(source, rendered))
            if float(before["start"]) != float(after["start"])
        ]
        self.assertEqual(changed_starts, [2])
        self.assertEqual(rendered[0]["start"], source[0]["start"])
        self.assertFalse(evidence["hook_delayed"])
        self.assertEqual([row["text"] for row in rendered], [row["text"] for row in source])
        self.assertTrue(all(row["template"] == "inner_dialogue" for row in rendered))
        self.assertLessEqual(int(evidence["delay_ms"]), 220)
        self.assertFalse(evidence["word_level_alignment_claimed"])
        self.assertEqual(evidence["extra_ai_calls"], 0)

    def test_micro_sound_bridge_reuses_same_payoff_and_only_prelaps_timing(self) -> None:
        source = self._events()
        bridged, evidence = montage.sound_bridge_events(source, "micro_story")
        self.assertEqual(len(bridged), len(source))
        self.assertEqual([row["text"] for row in bridged], [row["text"] for row in source])
        self.assertEqual(bridged[:-1], source[:-1])
        self.assertLess(float(bridged[-1]["start"]), float(source[-1]["start"]))
        self.assertEqual(float(bridged[-1]["end"]), float(source[-1]["end"]))
        self.assertLessEqual(int(evidence["prelap_ms"]), 220)
        self.assertEqual(evidence["extra_sfx_events"], 0)
        self.assertEqual(evidence["extra_ai_calls"], 0)

    def test_local_continuity_score_prefers_visually_nearer_cut(self) -> None:
        previous = {
            "luma": 0.55,
            "warmth": 0.10,
            "edge_density": 0.20,
            "center_x": 0.52,
            "center_y": 0.48,
            "motion": 0.14,
        }
        near = {
            "luma": 0.57,
            "warmth": 0.12,
            "edge_density": 0.22,
            "center_x": 0.50,
            "center_y": 0.50,
            "motion": 0.16,
        }
        far = {
            "luma": 0.10,
            "warmth": -0.70,
            "edge_density": 0.75,
            "center_x": 0.05,
            "center_y": 0.92,
            "motion": 0.90,
        }
        self.assertGreater(montage.continuity_score(previous, near), montage.continuity_score(previous, far))

    def test_match_offset_reuses_one_selected_asset_and_three_local_windows_only(self) -> None:
        previous_clip = Path("previous.mp4")
        raw = Path("candidate.mp4")
        previous_signature = {
            "luma": 0.50,
            "warmth": 0.10,
            "edge_density": 0.20,
            "center_x": 0.50,
            "center_y": 0.50,
            "motion": 0.10,
        }

        def signature(_video: Path, *, start_seconds: float, outgoing: bool):
            if outgoing:
                return previous_signature
            # Middle source window is intentionally the best adjacency match.
            if 1.4 <= start_seconds <= 1.6:
                return dict(previous_signature)
            return {
                "luma": 0.15,
                "warmth": -0.50,
                "edge_density": 0.70,
                "center_x": 0.10,
                "center_y": 0.85,
                "motion": 0.80,
            }

        with mock.patch.object(montage, "duration", side_effect=lambda path: 6.0 if Path(path) == raw else 3.0), \
                mock.patch.object(montage, "_boundary_signature", side_effect=signature):
            decision = montage.choose_match_offset(previous_clip, raw, 3.0)

        self.assertEqual(decision["status"], "selected")
        self.assertEqual(decision["candidate_windows"], 3)
        self.assertAlmostEqual(float(decision["start_offset_seconds"]), 1.5, places=2)
        self.assertEqual(decision["extra_ai_calls"], 0)
        self.assertEqual(decision["extra_stock_queries"], 0)

    def test_runtime_adapter_restores_certified_voice_owner_surface(self) -> None:
        original_retime = voice_owner.retime_events
        events = self._events()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def fake_base(output_dir, control_request, pre_gold, *, ledger):
                updated = dict(pre_gold)
                updated["timed_text_events"] = voice_owner.retime_events(
                    events,
                    source_seconds=12.0,
                    target_seconds=12.0,
                    first_event_max_seconds=3.0,
                )
                return updated

            updated = montage.apply_voice_owned_short_human(
                fake_base,
                root,
                {"approval_scope": "short_only", "kind": "short"},
                {"short_template": "why_reframe", "timed_text_events": events},
                ledger=object(),
            )
            report = json.loads((root / "human-editorial-montage-v1.json").read_text(encoding="utf-8"))

        self.assertIs(voice_owner.retime_events, original_retime)
        self.assertLess(float(updated["timed_text_events"][0]["end"]), 3.0)
        self.assertEqual(report["extra_text_ai_calls"], 0)
        self.assertEqual(report["extra_vision_ai_calls"], 0)
        self.assertEqual(report["extra_stock_queries"], 0)
        self.assertTrue(report["hard_cut_default_preserved"])
        self.assertFalse(report["speech_speed_changed"])
        self.assertFalse(report["words_changed"])

    def test_port_is_wired_and_montage_does_not_own_provider_transport(self) -> None:
        port_source = inspect.getsource(short_port.prepare_authoritative_short_for_gold)
        self.assertIn("human_montage.apply_voice_owned_short_human", port_source)
        self.assertIn("base_apply_voice_owned_short", port_source)
        self.assertIn("run_final_master_qc(output_dir)", port_source)

        module_source = inspect.getsource(montage)
        for forbidden in (
            "gemini_json_text(",
            "audit_video_preview(",
            "pexels_search_videos(",
            "pixabay_provider.search_videos(",
            "select_with_recovery(",
        ):
            self.assertNotIn(forbidden, module_source)
        self.assertNotIn("max_provider_attempts", module_source)
        self.assertNotIn("final_critic", module_source)


if __name__ == "__main__":
    unittest.main()
