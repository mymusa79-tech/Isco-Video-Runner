"""Regression tests for owner visual cohesion: outro, stock, palette and film shapes."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2 import media, owner_art
from clean_v2.owner_pack import LONG_TYPES


class ChatGPTAuthoredTypeAuthorityTests(unittest.TestCase):
    def test_all_12_chatgpt_chosen_templates_survive_zip_to_plan(self):
        import json
        from clean_v2 import owner_pack
        from scripts.test_production_package import imported_pack, manifest
        for fmt, types in (("short", owner_pack.SHORT_TYPES), ("film", owner_pack.LONG_TYPES)):
            for selected in types:
                with self.subTest(fmt=fmt, selected=selected):
                    with tempfile.TemporaryDirectory() as root:
                        package = imported_pack(manifest(fmt, template=selected))
                        output = Path(root)
                        owner_pack.build_artifacts(package, {"format": fmt}, output)
                        doc = json.loads((output / "production.json").read_text(encoding="utf-8"))
                        plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
                        self.assertEqual(doc["template"], selected)
                        chosen_in_plan = plan["short_template"] if fmt == "short" else plan["narrative_format"]
                        self.assertEqual(chosen_in_plan, selected)
                        self.assertEqual(doc["script"], package["production"]["script"])


class FilmTypeRhythmTests(unittest.TestCase):
    def test_every_owner_film_type_has_a_deliberate_pattern(self):
        self.assertEqual(set(LONG_TYPES), set(owner_art.SHOT_PATTERNS_BY_TYPE))
        self.assertEqual(set(LONG_TYPES), set(owner_art.PACE_BY_TYPE))
        plans = {}
        for kind in LONG_TYPES:
            factor, motion = owner_art.film_pace(kind)
            self.assertGreater(factor, 0)
            self.assertGreater(motion, 0)
            pieces = owner_art.segment_plan(
                60, 0, factor=factor,
                shot_pattern=owner_art.SHOT_PATTERNS_BY_TYPE[kind],
            )
            self.assertAlmostEqual(sum(pieces), 60.0, places=4)
            self.assertTrue(all(p > 1.5 for p in pieces))
            plans[kind] = tuple(round(p, 2) for p in pieces)
        self.assertGreaterEqual(len(set(plans.values())), 6)

    def test_long_dialogue_shape_uses_film_not_podcast_grade(self):
        self.assertEqual(owner_art.grade_profile("dialogue_qa", "film"), "film")
        self.assertEqual(owner_art.grade_profile("dialogue_qa", "podcast"), "podcast")
        self.assertEqual(owner_art.grade_profile("micro_story", "short"), "short")
        self.assertEqual(owner_art.grade_profile("ala_mahl", "short"), "ala_mahl")


class PackageNeighbourColorTests(unittest.TestCase):
    def test_neighbour_anchor_beats_global_episode_mean(self):
        paths = [Path(x) for x in (
            "a-owner.mp4", "a-stock.mp4", "a-owner-2.mp4",
            "b-owner.mp4", "b-stock.mp4", "b-owner-2.mp4",
        )]
        asset_rows = [
            {"local_file": name, "owner_approved": True} if "owner" in name
            else {"local_file": name, "source_actual": "stock_motion"}
            for name in [p.name for p in paths]
        ]
        def sample(path):
            name = Path(path).name
            if name.startswith("a-owner"):
                value = 55.0
            elif name.startswith("b-owner"):
                value = 185.0
            else:
                value = 120.0
            return media._RgbStats(value, value, value, 35.0, 35.0, 35.0)
        with mock.patch.object(media, "_sample_rgb_stats", side_effect=sample):
            filters = media._package_palette_match_filters(paths, asset_rows, fmt="film")
        self.assertEqual(set(filters), {str(paths[1]), str(paths[4])})
        self.assertNotEqual(filters[str(paths[1])], filters[str(paths[4])])
        self.assertIn("lutrgb=", filters[str(paths[1])])
        self.assertLess(media.PACKAGE_PALETTE_OFFSET_LIMIT, media.COLOR_MATCH_OFFSET_MAX)


class ApprovedAlternateStockTests(unittest.TestCase):
    def test_retries_only_one_editor_approved_alternate(self):
        calls = []
        def acquire(plan, directory, _fmt, _max, _estimated):
            active = plan["visual_story"]["beats"][0]["stock_query_en"]
            calls.append(active)
            directory.mkdir(parents=True, exist_ok=True)
            clip = directory / "visual-01.mp4"
            clip.write_bytes(b"clip")
            description = (
                "mountains and sunrise" if len(calls) == 1
                else "shoes walking on urban sidewalk"
            )
            return [clip], [{"source_actual": "stock_motion", "retrieval_description": description}]
        inner = mock.Mock()
        inner.acquire.side_effect = acquire
        beat = {"id": "b1", "section_id": "s1", "owner_stock": {
            "query_en": "person walking on city pavement",
            "query_alt_en": "shoes walking on urban sidewalk",
            "must_show": "shoes walking on urban sidewalk",
        }}
        with tempfile.TemporaryDirectory() as root:
            result, reason = owner_art.OwnerArtVisualSource(inner, Path(root))._try_stock(
                beat, {}, Path(root), "short",
            )
            self.assertEqual(reason, "")
            self.assertEqual(result[1]["stock_query_variant_used"], "approved_alternate")
            self.assertTrue(result[0].is_file())
        self.assertEqual(calls, [
            "person walking on city pavement",
            "shoes walking on urban sidewalk",
        ])


class ShortTemplateVisualGrammarTests(unittest.TestCase):
    def _decision(self, template, *, role="consequence", hold="idea_continues", owner_transition=""):
        paths = [Path("opening.mp4"), Path("next.mp4")]
        signals = {
            "opening.mp4": {
                "section_id": "s2", "environment_family": "small_room",
                "hold_reason": "idea_continues",
            },
            "next.mp4": {
                "section_id": "s2", "environment_family": "small_room",
                "hold_reason": hold, "shot_role": role,
                "owner_transition": owner_transition,
            },
        }
        return media._editorial_boundary_decisions(
            paths, ["s2", "s2"], signals, short_template=template,
        )[0]

    def test_each_short_type_has_observable_distinct_rule(self):
        self.assertEqual(self._decision("nidaa_short_micro_story")["reason"],
                         "micro_story_visible_event")
        self.assertEqual(self._decision("nidaa_short_inner_dialogue")["reason"],
                         "inner_dialogue_tension_hold")
        self.assertEqual(self._decision("nidaa_short_why_reframe")["reason"],
                         "why_reframe_visible_contrast")
        self.assertEqual(self._decision("nidaa_short_quote_reflection")["reason"],
                         "quote_reflection_held_meaning")
        self.assertEqual(self._decision("micro_story")["decision"], "CUT")
        self.assertEqual(self._decision("inner_dialogue")["decision"], "DISSOLVE")
        self.assertEqual(self._decision("quote_reflection", hold="payoff_landing")["reason"],
                         "quote_reflection_release")

    def test_approved_editor_cut_is_higher_priority_than_template(self):
        decision = self._decision("inner_dialogue", owner_transition="cut")
        self.assertEqual(decision["decision"], "CUT")
        self.assertEqual(decision["reason"], "approved_package_transition")
        decision = self._decision("why_reframe", owner_transition="dissolve")
        self.assertEqual(decision["decision"], "DISSOLVE")
        self.assertEqual(decision["reason"], "approved_package_transition")

    def test_unrelated_timeline_formats_do_not_pick_short_template(self):
        decision = self._decision("")
        self.assertNotIn("why_reframe", decision["reason"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires ffmpeg")
class ActualLastFrameHoldTests(unittest.TestCase):
    def test_terminal_hold_uses_last_displayed_frame_not_first_frame(self):
        with tempfile.TemporaryDirectory() as root:
            src = Path(root) / "story.mp4"
            dst = Path(root) / "hold.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=red:s=160x90:r=30:d=1",
                "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=30:d=1",
                "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
                "-map", "[v]", "-pix_fmt", "yuv420p", str(src),
            ], check=True, timeout=90)
            media._render_terminal_story_hold(src, dst, displayed_seconds=1.5, seconds=0.8)
            self.assertAlmostEqual(media.probe_duration(dst), 0.92, delta=0.13)
            probe = subprocess.run([
                "ffmpeg", "-v", "error", "-i", str(dst),
                "-frames:v", "1", "-vf", "scale=1:1,format=rgb24",
                "-f", "rawvideo", "-",
            ], check=True, capture_output=True, timeout=60).stdout
            self.assertGreater(probe[2], probe[0], "hold should stay blue at t=1.5")


if __name__ == "__main__":
    unittest.main()
