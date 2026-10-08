from __future__ import annotations

import copy
import json
import shutil
import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from clean_v2 import media, pipeline, visual_qa
from clean_v2.contracts import LONGFORM_NARRATIVE_FORMATS
from clean_v2.short_format import TEMPLATE_ORDER
from clean_v2.visual_story import contextual_intent, visual_review_context
from scripts import canonical_visual_evidence_v1 as evidence
from scripts import test_clean_v2 as qa_fixtures
from scripts.test_clean_v2_unified_visual_story import _brief
from scripts.test_planning_visual_repair_feedback import healthy_plan


BACKPACK = "person struggling to lift heavy backpack"
STATIC = "hands holding heavy stone versus light feather side by side"


def video(provider, index, description, *, portrait=True):
    width, height = (1080, 1920) if portrait else (640, 360)
    common = {"id": index, "duration": 5}
    if provider == "pexels":
        return dict(common, url="https://www.pexels.com/video/" + description.replace(" ", "-") + f"-{index}/",
                    video_files=[{"link": f"https://example.invalid/{index}.mp4", "width": width,
                                  "height": height, "file_type": "video/mp4"}])
    if provider == "pixabay":
        return dict(common, tags=description, pageURL=f"https://pixabay.com/videos/{index}/",
                    videos={"medium": {"url": f"https://example.invalid/{index}.mp4",
                                       "width": width, "height": height}})
    return dict(common, title=description, is_vertical=portrait, max_width=width, max_height=height,
                urls={"mp4": f"https://example.invalid/{index}.mp4"})


def photo(provider, index, description):
    if provider == "pexels":
        return {"id": index, "alt": description, "url": f"https://www.pexels.com/photo/{index}/",
                "width": 1080, "height": 1920, "src": {"large": f"https://example.invalid/{index}.jpg"}}
    return {"id": index, "tags": description, "pageURL": f"https://pixabay.com/photos/{index}/",
            "imageWidth": 1080, "imageHeight": 1920, "largeImageURL": f"https://example.invalid/{index}.jpg"}


class StockEvidenceSelectionTests(unittest.TestCase):
    def test_descriptive_fit_beats_popular_high_resolution_wrong_scene(self):
        # Run 185 selected a ski gondola for a struggling person with a backpack.
        # The relevant lower-resolution result must win inside the SAME response.
        for provider in ("pexels", "pixabay", "coverr"):
            with self.subTest(provider=provider):
                results = [video(provider, 1, "ski gondola snow mountain"),
                           video(provider, 2, BACKPACK, portrait=False)]
                response = {"videos" if provider == "pexels" else "hits": results}
                with mock.patch.object(media, "_read_secret", return_value="test"), mock.patch.object(
                    media, "_get_json", return_value=response
                ) as get:
                    selected = getattr(media.StockVisualSource(), "_" + provider)(BACKPACK, portrait=True)
                self.assertEqual(selected["asset_id"], "2")
                self.assertGreater(selected["metadata_semantic_score"], 0.8)
                self.assertEqual(get.call_count, 1)

    def test_recovery_ranks_whole_returned_page_before_six_item_limit(self):
        for provider in ("pexels", "pixabay", "coverr"):
            with self.subTest(provider=provider):
                results = [video(provider, i, "ski gondola snow mountain") for i in range(1, 12)]
                results.append(video(provider, 12, BACKPACK, portrait=False))
                response = {"videos" if provider == "pexels" else "hits": results}
                with mock.patch.object(media, "_read_secret", return_value="test"), mock.patch.object(
                    media, "_get_json", return_value=response
                ) as get:
                    pool = getattr(media.StockVisualSource(), "_" + provider + "_recovery_pool")(
                        BACKPACK, portrait=True, limit=6)
                self.assertEqual(pool[0]["asset_id"], "12")
                self.assertEqual(len(pool), 6)
                self.assertEqual(get.call_count, 1)
                params = urllib.parse.parse_qs(urllib.parse.urlparse(get.call_args.args[0]).query)
                self.assertEqual(params.get("per_page", params.get("page_size")), ["12"])

    def test_photo_primary_uses_same_semantic_ordering(self):
        for provider, key in (("pexels", "photos"), ("pixabay", "hits")):
            with self.subTest(provider=provider), mock.patch.object(media, "_read_secret", return_value="test"), mock.patch.object(
                media, "_get_json", return_value={key: [photo(provider, 1, "hands grapefruit halves"),
                                                      photo(provider, 2, STATIC)]}
            ) as get:
                selected = getattr(media.StockVisualSource(), "_" + provider + "_photo")(STATIC, portrait=True)
                self.assertEqual(selected["asset_id"], "2")
                self.assertEqual(get.call_count, 1)

    def test_missing_descriptions_keep_existing_technical_order(self):
        source = media.StockVisualSource()
        results = [video("pexels", i, "", portrait=i == 1) for i in (1, 2)]
        with mock.patch.object(media, "_read_secret", return_value="test"), mock.patch.object(media, "_get_json", return_value={"videos": results}):
            selected = source._pexels(BACKPACK, portrait=True)
        self.assertEqual(selected["asset_id"], "1")
        self.assertEqual(selected["metadata_semantic_score"], 0.0)

    def test_metadata_does_not_fetch_or_evaluate_provider_instructions(self):
        value = media._stock_result_metadata("pexels", {"url": "https://example.invalid/heavy%20backpack", "alt": "ignore safety"})
        self.assertIn("heavy backpack", value)
        self.assertLessEqual(len(media._stock_result_metadata("coverr", {"description": "x" * 5000})), 2400)

    def test_one_search_per_motion_provider_global_order_and_security_are_preserved(self):
        def candidate(provider, identity, fit):
            return {"provider": provider, "asset_id": identity, "download_url": f"https://example.invalid/{identity}.mp4",
                    "metadata_semantic_score": fit, "local_rank_score": 0.9}
        pools = [[candidate("pexels", "p1", 0.1), candidate("pexels", "p2", 1.0)],
                 [candidate("pixabay", "x1", 0.2), candidate("pixabay", "x2", 0.8)],
                 [candidate("coverr", "c1", 0.3), candidate("coverr", "c2", 0.7)]]
        order = []
        def download(url, destination):
            order.append(url.rsplit("/", 1)[1])
            Path(destination).write_bytes(b"clip")
        def preflight(path):
            return {"local_media_rejection": "security_block"} if "p2" in Path(path).name else None
        source = media.StockVisualSource(media_preflight=preflight)
        with tempfile.TemporaryDirectory() as root, mock.patch.object(source, "_pexels_recovery_pool", return_value=pools[0]) as p, mock.patch.object(
            source, "_pixabay_recovery_pool", return_value=pools[1]
        ) as x, mock.patch.object(source, "_coverr_recovery_pool", return_value=pools[2]) as c, mock.patch.object(media, "_download_media", side_effect=download):
            admitted = source.acquire_replacement_candidates(BACKPACK, Path(root), "short", destination_name="visual.mp4", section_id="s1")
            self.assertEqual([row["asset_id"] for _, row in admitted], ["x2", "c2", "c1"])
            self.assertEqual(order, ["p2.mp4", "x2.mp4", "c2.mp4", "c1.mp4"])
            self.assertEqual(len(admitted), 3)
            self.assertFalse(any("p2" in path.name for path in Path(root).iterdir()))
        for search in (p, x, c):
            self.assertEqual(search.call_count, 1)
            self.assertEqual(search.call_args.kwargs["limit"], 6)

    def test_still_recovery_reuses_photos_without_video_or_ai_call_for_all_formats(self):
        for fmt in ("short", "film", "podcast"):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as root:
                sequence = []
                source = media.StockVisualSource(media_preflight=lambda path: sequence.append("security"),
                                                 media_transform=lambda path: sequence.append("transform") or path)
                def get(url, **kwargs):
                    sequence.append("search")
                    if "pexels" in url:
                        return {"photos": [photo("pexels", 1, "grapefruit"), photo("pexels", 2, STATIC)]}
                    return {"hits": [photo("pixabay", 3, STATIC)]}
                def render(image, dest, **kwargs):
                    self.assertTrue(Path(image).is_file())
                    self.assertEqual(kwargs["fmt"], fmt)
                    sequence.append("render")
                    Path(dest).write_bytes(b"rendered photo")
                with mock.patch.object(media, "_read_secret", return_value="test"), mock.patch.object(media, "_get_json", side_effect=get) as search, mock.patch.object(
                    media, "_download_media", side_effect=lambda url, dest: Path(dest).write_bytes(b"photo")
                ), mock.patch.object(media, "_render_ai_still", side_effect=render), mock.patch.object(
                    source, "_pexels_recovery_pool", side_effect=AssertionError("extra video search")
                ), mock.patch.object(source, "_pixabay_recovery_pool", side_effect=AssertionError("extra video search")), mock.patch.object(
                    source, "_coverr_recovery_pool", side_effect=AssertionError("extra video search")
                ):
                    admitted = source.acquire_replacement_candidates(STATIC, Path(root), fmt, destination_name="visual.mp4",
                                                                     section_id="s1", source_preference="stock_still", max_candidates=50)
                self.assertEqual(search.call_count, 2)
                self.assertEqual([row["asset_id"] for _, row in admitted], ["3", "2"])
                self.assertTrue(all(row["source_actual"] == "stock_still" for _, row in admitted))
                self.assertEqual(sequence, ["search", "search", "render", "security", "transform", "render", "security", "transform"])
                self.assertFalse(list(Path(root).glob("*.jpg")))

    def test_still_recovery_excludes_already_selected_photo_with_both_identity_forms(self):
        source = media.StockVisualSource()
        with tempfile.TemporaryDirectory() as root, mock.patch.object(media, "_read_secret", return_value="test"), mock.patch.object(
            media, "_get_json", side_effect=[{"photos": [photo("pexels", 1, STATIC)]}, {"hits": [photo("pixabay", 2, STATIC)]}]
        ) as search:
            admitted = source.acquire_replacement_candidates(STATIC, Path(root), "film", destination_name="visual.mp4", section_id="s1",
                                                             source_preference="stock_still", exclude_provider="pexels", exclude_asset_id=1,
                                                             exclude_assets=[("pixabay", 2)])
        self.assertEqual(admitted, [])
        self.assertEqual(search.call_count, 2)


class VisualProofContextTests(unittest.TestCase):
    def test_run185_full_action_and_both_required_states_survive_review_prompt(self):
        story = {"beats": [{"id": "b3", "role": "body", "shot_intent": BACKPACK + " while another carries small bag effortlessly",
                            "meaning_target": "العبء لا يتساوى مع حجم المهمة", "semantic_must_have": ["visible effort carrying heavy load", "contrasting light load"],
                            "semantic_should_avoid": ["ski gondola"]}]}
        before = copy.deepcopy(story)
        context = visual_review_context(story, "b3", "unrelated retrieval query")
        prompt = evidence.canonical_visual_prompt(narration_context="العبء", intended_visual=context)
        for text in (BACKPACK, "carries small bag effortlessly", "visible effort carrying heavy load", "contrasting light load"):
            self.assertIn(text, prompt)
        self.assertNotIn("unrelated retrieval query", context)
        self.assertEqual(story, before)
        self.assertLessEqual(len(context), 1600)
        self.assertLessEqual(len(contextual_intent(story, "b3", BACKPACK)), 300)

    def test_actual_replacement_family_reaches_next_review_instead_of_old_plan(self):
        story = {"beats": [{"id": "b1", "shot_intent": "hands typing laptop"},
                            {"id": "b2", "shot_intent": "hands writing notebook", "semantic_must_have": ["completed entry"]}]}
        audit = visual_qa._apply_observed_visual_proof({"status": "pass", "relevance": .9,
                    "reason": "OBSERVED: hand writing notes in notebook; PROOF: matched; FACE: none; entry visible"})
        self.assertEqual(audit["observed_action_family"], "stationery")
        context = visual_review_context(story, "b2", "", previous_observation=audit["observed_visual"])
        self.assertIn("Previous accepted observation: hand writing notes in notebook", context)
        self.assertNotIn("hands typing laptop", context)

    def test_static_comparison_and_motion_choose_appropriate_existing_recovery_path(self):
        self.assertEqual(visual_qa._recovery_source_preference({}, STATIC), "stock_still")
        self.assertEqual(visual_qa._recovery_source_preference({}, "close up " + STATIC), "stock_still")
        self.assertEqual(visual_qa._recovery_source_preference({}, "hands placing heavy stone versus light feather"), "stock_motion")
        self.assertEqual(visual_qa._recovery_source_preference({"source_preference": "ai_still"}, BACKPACK), "ai_still")
        self.assertEqual(visual_qa._recovery_source_preference({}, BACKPACK), "stock_motion")

    def test_contradicted_or_missing_proof_cannot_pass_or_retain_best_available(self):
        for proof in ("missing", "contradicted", "uncertain"):
            with self.subTest(proof=proof):
                raw = {"status": "pass", "relevance": .95, "visual_quality": .95,
                       "reason": f"OBSERVED: hand writing on written pages; PROOF: {proof}; FACE: none; blank frozen scene absent"}
                before = dict(raw)
                result = visual_qa._apply_observed_visual_proof(raw)
                self.assertEqual(result["status"], "block")
                self.assertLess(result["relevance"], .65)
                self.assertFalse(visual_qa._can_retain_safe_best_available_primary(primary_audit=result, primary_floor=result["relevance"],
                                                                                best_recovery_floor=.2, is_hook=False))
                self.assertEqual(raw, before)

    def test_recognizable_or_uncertain_face_overrides_inconsistent_false_boolean(self):
        for face in ("recognizable", "uncertain"):
            with self.subTest(face=face):
                result = visual_qa._apply_observed_visual_proof({"status": "pass", "relevance": .9, "identifiable_person": False,
                            "reason": f"OBSERVED: woman with clear facial features; PROOF: matched; FACE: {face}; desk scene"})
                self.assertEqual(result["status"], "block")
                if face == "recognizable":
                    self.assertTrue(result["identifiable_person"])

    def test_matched_proof_never_promotes_block_or_low_score_and_legacy_reason_is_preserved(self):
        for status, score in (("block", .95), ("pass", .4)):
            result = visual_qa._apply_observed_visual_proof({"status": status, "relevance": score,
                        "reason": "OBSERVED: hands sorting objects; PROOF: matched; FACE: none; clear scene"})
            self.assertEqual((result["status"], result["relevance"]), (status, score))
        legacy = {"status": "block", "relevance": .2, "reason": "legacy grounded rejection"}
        self.assertEqual(visual_qa._apply_observed_visual_proof(legacy), legacy)

    def test_current_and_recovery_are_reviewed_against_same_editorial_truth(self):
        class Harness(qa_fixtures.VisualQASemanticRecoveryTests):
            audit_number = 0
            def _audit(self, **kwargs):
                result = super()._audit(**kwargs)
                self.audit_number += 1
                result["reason"] = ("OBSERVED: blank screen phone; PROOF: missing; FACE: none; no scrolling visible"
                                    if self.audit_number == 1 else
                                    "OBSERVED: hands avoiding open laptop task while scrolling phone; PROOF: matched; FACE: none; avoidance visible")
                return result
        outcome = Harness()._run_case(primary_status="pass", primary_relevance=.95, recovery_relevance=.92)
        self.assertEqual(outcome["commit_calls"], 1)
        self.assertEqual(outcome["audit_calls"], 2)
        self.assertEqual(outcome["audits"][0]["status"], "block")
        self.assertEqual(outcome["audits"][0]["observed_proof_status"], "missing")
        self.assertEqual(outcome["audits"][1]["observed_proof_status"], "matched")
        self.assertEqual(outcome["intended_visual_calls"][0], outcome["intended_visual_calls"][1])
        self.assertIn(qa_fixtures.VisualQASemanticRecoveryTests.ORIGINAL_QUERY, outcome["intended_visual_calls"][1])
        self.assertNotIn(qa_fixtures.VisualQASemanticRecoveryTests.ALTERNATE_QUERY, outcome["intended_visual_calls"][1])


class DisplayWindowEvidenceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
    def test_actual_frames_match_visible_prefix_and_center_crop_not_unused_later_action(self):
        # The source has red at its left edge and turns green AFTER the two
        # seconds shown. Neither can be used as proof in this portrait slot.
        def rgb_mean(path):
            raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1",
                                           "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
            return tuple(sum(raw[channel::3]) / len(raw[channel::3]) for channel in range(3))
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / "source.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=640x360:r=10:d=2",
                            "-f", "lavfi", "-i", "color=c=green:s=640x360:r=10:d=4", "-filter_complex",
                            "[0:v]drawbox=x=0:y=0:w=100:h=360:color=red:t=fill[a];[a][1:v]concat=n=2:v=1:a=0[v]",
                            "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(source)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            bundle = evidence.build_canonical_visual_evidence(source, root / "bundle", narration_context="ctx", intended_visual="blue scene",
                                                               duration_limit_seconds=2.0, display_aspect_ratio=(9, 16))
            manifest = json.loads((root / "bundle" / "evidence.json").read_text())
            self.assertEqual(manifest["sampling_window_seconds"], 2.0)
            self.assertEqual(manifest["frame_timestamps_seconds"], [.36, 1.0, 1.64])
            self.assertEqual(manifest["display_aspect_ratio"], [9, 16])
            self.assertEqual(len(bundle.frame_sha256), 3)
            for frame in bundle.frame_paths:
                red, green, blue = rgb_mean(frame)
                self.assertLess(red, 10)
                self.assertLess(green, 10)
                self.assertGreater(blue, 230)
            rendered = media._trim_and_grade_clip(source, root / "rendered.mp4", width=90, height=160, seconds=2.0, grade_filter="")
            evidence._extract_frame(rendered, root / "rendered.jpg", 1.0)
            self.assertTrue(all(abs(a - b) < 4 for a, b in zip(rgb_mean(bundle.frame_paths[1]), rgb_mean(root / "rendered.jpg"))))

    def test_shorter_source_never_extrapolates_beyond_real_pixels(self):
        def extract(source, dest, timestamp):
            Path(dest).write_bytes(str(timestamp).encode())
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "clip.mp4"
            source.write_bytes(b"clip")
            with mock.patch.object(evidence, "_duration", return_value=2.0), mock.patch.object(evidence, "_extract_frame", side_effect=extract) as frames:
                evidence.build_canonical_visual_evidence(source, Path(root) / "bundle", narration_context="ctx", intended_visual="intent", duration_limit_seconds=10.0)
            self.assertEqual([c.args[2] for c in frames.call_args_list], [.36, 1.0, 1.64])

    def test_invalid_window_cannot_create_visual_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "clip.mp4"
            source.write_bytes(b"clip")
            with mock.patch.object(evidence, "_duration", return_value=6.0), mock.patch.object(evidence, "_extract_frame") as frames:
                for limit in (0, -1, float("nan"), float("inf")):
                    with self.subTest(limit=limit), self.assertRaises(ValueError):
                        evidence.build_canonical_visual_evidence(source, Path(root) / "bundle", narration_context="ctx", intended_visual="intent", duration_limit_seconds=limit)
                frames.assert_not_called()

    def test_measured_short_window_reuses_renderer_weights_and_hook_cap(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "visual-story.json").write_text("{}")
            rights = [{"local_file": f"v{i}.mp4", "section_id": "s1" if i < 4 else "s2"} for i in range(1, 6)]
            (root / "rights-manifest.json").write_text(json.dumps({"assets": rights}))
            (root / "timeline-first.json").write_text(json.dumps({"status": "pass", "voice_seconds_measured": 30,
                "section_events": [{"section_id": "s1", "start": 0, "end": 18}, {"section_id": "s2", "start": 18, "end": 30}],
                "identity_events": [{"kind": "hook", "start": 0, "end": 6}]}))
            windows = visual_qa._review_display_durations(root, rights, "short")
            self.assertAlmostEqual(windows["v1.mp4"], 2.0)
            self.assertAlmostEqual(windows["v2.mp4"], 2.0)
            self.assertAlmostEqual(windows["v3.mp4"], 14.36)
            self.assertAlmostEqual(windows["v4.mp4"], 6.12)
            for fmt in ("film", "podcast"):
                windows = visual_qa._review_display_durations(root, rights, fmt)
                self.assertAlmostEqual(windows["v1.mp4"], 6.12)
                self.assertAlmostEqual(windows["v5.mp4"], 6.12)

    def test_primary_and_replacement_keep_same_slot_window(self):
        with mock.patch.object(visual_qa, "_review_display_durations", return_value={"visual-03.mp4": 2.0}):
            outcome = qa_fixtures.VisualQASemanticRecoveryTests()._run_case(recovery_relevance=.92)
        self.assertEqual([a["review_duration_limit_seconds"] for a in outcome["audits"]], [2.0, 2.0])
        self.assertEqual([a["review_display_aspect_ratio"] for a in outcome["audits"]], [[16, 9], [16, 9]])


class AllTemplatesVisualEvidenceTests(unittest.TestCase):
    def check_template(self, fmt, template):
        brief = _brief(fmt)
        profile = (pipeline.select_short_template(brief) if fmt == "short" else pipeline._select_longform_narrative_profile(brief))
        profile["template" if fmt == "short" else "narrative_format"] = template
        selector = "select_short_template" if fmt == "short" else "_select_longform_narrative_profile"
        with mock.patch.object(pipeline, selector, return_value=profile):
            prompt = pipeline._planning_prompt(brief)
            plan = pipeline._validate_plan_for_brief(healthy_plan(fmt), brief)
        self.assertIn("STOCK FEASIBILITY", prompt)
        self.assertEqual(plan["short_template" if fmt == "short" else "narrative_format"], template)
        original = copy.deepcopy(plan["visual_story"])
        for beat in plan["visual_story"]["beats"]:
            context = visual_review_context(plan["visual_story"], beat["id"], "alternate retrieval syntax")
            final = evidence.canonical_visual_prompt(narration_context="تعليق مرتبط بالمشهد", intended_visual=context)
            self.assertIn(beat["shot_intent"][:220], final)
            self.assertIn("Must show:", final)
            self.assertNotIn("alternate retrieval syntax", final)
            self.assertIn("OBSERVED:", final)
        self.assertEqual(plan["visual_story"], original)


def template_case(fmt, template):
    def test(self):
        self.check_template(fmt, template)
    return test


for _fmt, _templates in (("short", TEMPLATE_ORDER), ("film", sorted(LONGFORM_NARRATIVE_FORMATS)), ("podcast", ("dialogue_qa",))):
    for _template in _templates:
        setattr(AllTemplatesVisualEvidenceTests, f"test_{_fmt}_{_template}", template_case(_fmt, _template))


if __name__ == "__main__":
    unittest.main()
