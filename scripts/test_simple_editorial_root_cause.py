"""Run128: measured ownership, observed pixels, Cairo shaping and truthful release."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from clean_v2 import media, simple_mode, short_timed_text


class MeasuredSceneOwnershipTests(unittest.TestCase):
    def test_podcast_first_answer_gets_a_distinct_source_after_identity(self):
        from clean_v2.identity_sequence import PRAYER_SENTENCE
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {simple_mode.SIMPLE_MODE_ENV: "1"}):
            root = Path(temp); visuals = root / "visuals"
            timeline = {"identity_events": [{"kind": "hook", "start": 0, "end": 2}, {"kind": "topic", "start": 10, "end": 20}],
                        "section_events": [{"section_id": "s1", "start": 0, "end": 20}]}
            (root / "timeline-first.json").write_text(json.dumps(timeline))
            (root / "script.json").write_text(json.dumps({"sections": [{"id": "s1", "narration": f"A: كيف أبدأ؟ {PRAYER_SENTENCE} B: اختر هدفًا واحدًا اليوم."}]}))
            plan = {"sections": [{"id": "s1", "visual_query_en": "hand moving one object"}],
                    "visual_story": {"beats": [{"id": "b1", "section_id": "s1", "role": "hook", "shot_intent": "hand moving one object", "stock_query_en": "hand moving one object"}]}}
            source = media.StockVisualSource()
            candidate = {"provider": "pexels", "asset_id": "1", "download_url": "https://example.com/a.mp4"}
            def write(_url, path): Path(path).write_bytes(b"fixture")
            def local(path, **_): Path(path).write_bytes(b"local-fixture"); return path
            with mock.patch.object(source, "_pexels", return_value=candidate), mock.patch.object(source, "_pixabay", return_value=None), \
                 mock.patch.object(source, "_coverr", return_value=None), mock.patch.object(media, "_download_media", side_effect=write), \
                 mock.patch.object(media, "create_fallback_visual", side_effect=local):
                paths, rights = source.acquire(plan, visuals, "podcast", 5)
            self.assertEqual([r["role"] for r in rights], ["hook", "body"])
            self.assertEqual(rights[1]["provider"], "generated_local")
            (root / "rights-manifest.json").write_text(json.dumps({"assets": rights}))
            slots = media._voice_owned_visual_schedule(root, paths, timeline)
            self.assertEqual([(s["start"], s["end"]) for s in slots], [(0, 2), (10, 20)])
            self.assertNotEqual(slots[0]["local_file"], slots[1]["local_file"])
    def test_identity_is_excluded_and_topic_starts_with_its_own_scene_in_all_formats(self):
        for fmt in ("short", "film", "podcast"):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                paths = [root / f"{i}.mp4" for i in range(7)]
                sections = ["s1"] * 3 + ["s2"] * 2 + ["s3"] * 2
                rows = [{"local_file": p.name, "section_id": s, "beat_id": f"b{i}",
                         "role": "hook" if s == "s1" else "body"} for i, (p, s) in enumerate(zip(paths, sections))]
                (root / "rights-manifest.json").write_text(json.dumps({"assets": rows}))
                timeline = {"identity_events": [{"kind": "hook", "start": 0, "end": 7.88},
                                               {"kind": "topic", "start": 18.22, "end": 44.46}],
                            "section_events": [{"section_id": "s1", "start": 0, "end": 18.22},
                                               {"section_id": "s2", "start": 18.22, "end": 36.54},
                                               {"section_id": "s3", "start": 36.54, "end": 45.71}]}
                (root / "timeline-first.json").write_text(json.dumps(timeline))
                slots = media._voice_owned_visual_schedule(root, paths, timeline)
                self.assertEqual(len(slots), 7)
                self.assertAlmostEqual(slots[2]["end"], round(7.88 * 30) / 30)
                self.assertAlmostEqual(slots[3]["start"], round(18.22 * 30) / 30)
                self.assertEqual(slots[3]["local_file"], paths[3].name)
                self.assertTrue(all(s["end"] <= 7.9 or s["start"] >= 18.2 for s in slots))
                self.assertAlmostEqual(slots[-1]["end"], round(44.46 * 30) / 30)
                self.assertEqual(len({s["local_file"] for s in slots}), 7)

    def test_authored_idea_anchor_beats_equal_shares_without_changing_section_end(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = [root / "a.mp4", root / "b.mp4"]
            (root / "rights-manifest.json").write_text(json.dumps({"assets": [
                {"local_file": "a.mp4", "section_id": "s2", "role": "body"},
                {"local_file": "b.mp4", "section_id": "s2", "role": "body", "writer_anchor_ar": "اختر هدفًا واحدًا اليوم."}]}))
            (root / "script.json").write_text(json.dumps({"sections": [{"id": "s2", "narration": "لا تجعل مقارنة نفسك بالآخرين تسرق منك يومك. اختر هدفًا واحدًا اليوم."}]}))
            timeline = {"identity_events": [{"kind": "topic", "start": 10, "end": 34}],
                        "section_events": [{"section_id": "s2", "start": 10, "end": 34}]}
            (root / "timeline-first.json").write_text(json.dumps(timeline))
            slots = media._voice_owned_visual_schedule(root, paths, timeline)
            self.assertGreater(slots[0]["duration_seconds"], slots[1]["duration_seconds"])
            self.assertEqual(slots[-1]["end"], 34)


class SinglePassEvidenceTests(unittest.TestCase):
    @staticmethod
    def audit(reason="OBSERVED: hands moving one object; PROOF: matched; FACE: none"):
        return {"status": "pass", "relevance": .9, "visual_quality": .9, "reason": reason,
                **{k: False for k in ("identifiable_person", "sensitive_trait_implication_risk", "prominent_logo_or_brand",
                                     "cultural_conflict", "cultural_islamic_suitability_risk", "advertiser_conflict", "obvious_synthetic_or_visual_artifact")}}

    def run_qa(self, audits, count=1, credential=True, persisted_story=False, twice=False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / "visuals").mkdir()
            rights = []
            for index in range(count):
                name = f"{index}.mp4"; (root / "visuals" / name).write_bytes(b"source")
                rights.append({"local_file": name, "section_id": "s2", "beat_id": f"b{index}", "provider": "pexels",
                               "source_actual": "stock_motion", "meaning_target": "المقارنة بين تقدم شخصين"})
            (root / "rights-manifest.json").write_text(json.dumps({"assets": rights}))
            if persisted_story:
                (root / "visual-story.json").write_text(json.dumps({"beats": [{"id": "b0", "section_id": "s2", "role": "body", "shot_intent": "hand moving one object", "semantic_must_have": ["moving object"]}]}))
            evidence = SimpleNamespace(prompt_hash="p", frame_sha256=("f1", "f2", "f3"))
            with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "unit-test" if credential else ""}), \
                 mock.patch("scripts.canonical_visual_evidence_v1.build_canonical_visual_evidence", return_value=evidence), \
                 mock.patch("scripts.canonical_visual_evidence_v1.audit_gemini_canonical_evidence", side_effect=audits) as review, \
                 mock.patch.object(media, "create_fallback_visual") as fallback:
                report = simple_mode.simple_visual_qa(output_dir=root, plan={}, script={}, rights=rights, fmt="film")
                if twice:
                    report = simple_mode.simple_visual_qa(output_dir=root, plan={}, script={}, rights=rights, fmt="film")
            return report, rights, review.call_count, fallback.call_count

    def test_observed_missing_proof_face_and_artifacts_replace_without_retry(self):
        cases = [self.audit("OBSERVED: landscape only; PROOF: missing; FACE: none"),
                 self.audit("OBSERVED: fashion model posing; PROOF: matched; FACE: recognizable"),
                 {**self.audit(), "obvious_synthetic_or_visual_artifact": True},
                 self.audit("metadata looks relevant"),
                 self.audit("OBSERVED: woman posing on a catwalk; PROOF: matched; FACE: none")]
        for audit in cases:
            with self.subTest(audit=audit["reason"]):
                report, rights, calls, fallbacks = self.run_qa([audit])
                self.assertEqual((calls, fallbacks), (1, 1))
                self.assertEqual(rights[0]["visual_verification"], "local_symbolic")
                self.assertEqual(report["observed_matched_clips"], 0)
                self.assertEqual(report["recovery_searches"], 0)

    def test_outage_disables_later_calls_and_never_approves_metadata(self):
        report, rights, calls, fallbacks = self.run_qa([TimeoutError("timeout")], count=3)
        self.assertEqual((calls, fallbacks), (1, 3))
        self.assertTrue(all(r["provider"] == "generated_local" for r in rights))
        self.assertEqual(report["observed_matched_clips"], 0)

    def test_proven_safe_action_is_retained_despite_subjective_quality_score(self):
        audit = {**self.audit(), "status": "block", "visual_quality": .4}
        report, rights, calls, fallbacks = self.run_qa([audit])
        self.assertEqual((calls, fallbacks), (1, 0))
        self.assertEqual(rights[0]["visual_verification"], "observed_matched")
        self.assertEqual(report["observed_matched_clips"], 1)

    def test_missing_credential_uses_disclosed_local_scene_without_any_provider_call(self):
        report, rights, calls, fallbacks = self.run_qa([], credential=False)
        self.assertEqual((calls, fallbacks), (0, 1))
        self.assertEqual(rights[0]["local_visual_recipe"], "independent_progress")

    def test_persisted_story_context_and_exact_evidence_are_reused_without_second_call(self):
        report, rights, calls, fallbacks = self.run_qa([self.audit()], persisted_story=True, twice=True)
        self.assertEqual((calls, fallbacks), (1, 0))
        self.assertEqual(report["observed_matched_clips"], 1)


class ArabicRegressionTests(unittest.TestCase):
    def test_arabic_marks_hamzas_numbers_and_all_words_survive_two_line_layout(self):
        texts = ["لست في سباق مع أحد.", "بمنتصف طريق الآخرين،", "أَؤَجِّلُ خطوةً؟ ٣٪ و١٢٣؛ إِلى الآن!"]
        events = [{"start": i, "end": i + 1, "text": t, "role": "hook" if i == 0 else "payoff" if i == 2 else "beat"} for i, t in enumerate(texts)]
        with mock.patch.dict(os.environ, {simple_mode.SIMPLE_MODE_ENV: "1"}):
            ass = short_timed_text.build_rich_ass(events)
            for text in texts:
                rows, _ = short_timed_text._caption_rows_and_size(text, fmt="short", font_size=116)
                self.assertLessEqual(len(rows), 2)
                self.assertEqual(" ".join(rows), short_timed_text._clean(text))
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp); (root / "render.mp4").write_bytes(b"video")
                with mock.patch.object(short_timed_text.subprocess, "run", return_value=SimpleNamespace(stderr="")) as run:
                    short_timed_text.render_progressive_text(video=root / "in.mp4", events=events,
                                                             srt_path=root / "t.srt", output=root / "render.mp4")
                command = run.call_args.args[0]
                self.assertIn("shaping=complex", command[command.index("-vf") + 1])
        self.assertIn("Cairo", ass)
        self.assertIn(",5,70,70,0,-1", ass)  # libass automatic RTL paragraph direction.


if __name__ == "__main__":
    unittest.main()
