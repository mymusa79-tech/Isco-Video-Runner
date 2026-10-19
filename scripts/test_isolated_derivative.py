from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from clean_v2 import derived_short, owner_pack
from clean_v2.contracts import atomic_write_json, compute_brief_sha256
from clean_v2.media import _gemini38_dialogue_turns, probe_duration
from clean_v2.pipeline import CleanV2Pipeline
from scripts import clean_v2_release_delivery as delivery, telegram_clean_v2_control as control
from scripts.test_clean_v2 import _passing_audio_mastering, _passing_final_master_qc, _passing_visual_qa
from scripts.test_clean_v2_release_delivery import FakeRunner
from scripts.test_production_package import manifest
from scripts.test_ready_production_e2e import LocalVoice


def parent_fixture(root: Path, *, real_media=False) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    doc = manifest("podcast")
    question, answer = [t["text"] for t in doc["script"]["sections"][1]["turns"]]
    doc["derivative"] = {"start_text": question, "end_text": answer}
    rows = [{"section_id": "s2", "chunk": i, "role": "topic", "start": a, "end": b,
             "duration_seconds": b-a, "file": f"audio/{i}.wav"}
            for i, a, b in ((1, 2.0, 6.0), (2, 6.0, 14.0))]
    atomic_write_json(root / "production.json", doc)
    atomic_write_json(root / "timeline-first.json", {"status": "pass", "format": "podcast", "audio_units": rows})
    atomic_write_json(root / "voice-sections.json", {"sections": [{"id": "s2", "chunks": [
        {"chunk": 1, "role": "topic", "text": "A: " + question},
        {"chunk": 2, "role": "topic", "text": "B: " + answer}]}]})
    atomic_write_json(root / "script.json", {"sections": [{"id": s["id"], "narration": " ".join(t["speaker"]+": "+t["text"] for t in s["turns"])} for s in doc["script"]["sections"]]})
    atomic_write_json(root / "run-manifest.json", {"status": "pass", "format": "podcast", "topic": doc["title"]})
    atomic_write_json(root / "final-master-qc.json", {"status": "pass"})
    (root / "cover.jpg").write_bytes(b"fixture cover")
    if real_media:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=0x0B1220:s=480x270:r=30:d=16", "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=24000:duration=16",
                        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(root/"final.mp4")], check=True)
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(root/"final.mp4"),
                        "-vn", "-c:a", "pcm_s16le", str(root/"narration-mastered.wav")], check=True)
    else:
        (root / "final.mp4").write_bytes(b"ACCEPTED ORIGINAL VIDEO")
        (root / "narration-mastered.wav").write_bytes(b"ACCEPTED ORIGINAL AUDIO")
    return doc


class DerivativeTests(unittest.TestCase):
    def test_optional_boundary_preparation_failure_keeps_original_voice_pass(self):
        from clean_v2.pipeline import _synthesize_sectioned_voice
        from scripts.test_production_package import imported_pack
        doc = manifest("podcast")
        question, answer = [t["text"] for t in doc["script"]["sections"][1]["turns"]]
        doc["derivative"] = {"start_text": question, "end_text": answer}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); voice = LocalVoice()
            script = owner_pack.build_artifacts(imported_pack(doc), {"format": "podcast"}, root)
            before = json.dumps(script, ensure_ascii=False)
            with mock.patch.object(derived_short, "split_voice_units", side_effect=RuntimeError("optional boundary bug")):
                result = _synthesize_sectioned_voice(voice, script["sections"], root/"narration.wav", fmt="podcast")
            self.assertTrue((root/"narration.wav").is_file())
            self.assertEqual(result["voice_provider"], "gemini-3.8:Charon")
            self.assertEqual(json.dumps(script, ensure_ascii=False), before)

    def test_first_voice_pass_measures_requested_text_without_a_second_synthesis(self):
        from clean_v2.pipeline import _synthesize_sectioned_voice
        from clean_v2.timeline_first import build_voice_owned_timeline
        from scripts.test_production_package import imported_pack
        doc = manifest("podcast")
        question, answer = [t["text"] for t in doc["script"]["sections"][1]["turns"]]
        end = answer.split(". ")[-1]
        doc["derivative"] = {"start_text": question, "end_text": end}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); voice = LocalVoice()
            script = owner_pack.build_artifacts(imported_pack(doc), {"format": "podcast"}, root)
            _synthesize_sectioned_voice(voice, script["sections"], root/"narration.wav", fmt="podcast")
            count = len(voice.spoken)
            timeline = build_voice_owned_timeline(output_dir=root, narration_path=root/"narration.wav", fmt="podcast")
            atomic_write_json(root/"timeline-first.json", timeline)
            rows = derived_short._audio_rows(root)
            selected, reason = derived_short._selection({"candidate": doc["derivative"], "rows": rows, "format": "podcast"}, doc)
            self.assertIsNotNone(selected, reason)
            self.assertEqual(count, len(voice.spoken))
            self.assertTrue(selected["text"].startswith("A: "+question))
            self.assertTrue(selected["text"].endswith(end))
            self.assertEqual(selected["start"], next(r["start"] for r in rows if r["section_id"] == "s2"))

    def test_pipeline_commits_primary_success_before_an_unexpected_derivative_exception(self):
        from scripts.test_production_package import imported_pack
        doc = manifest("film"); pack = imported_pack(doc)
        brief = {"approved_by_user": True, "approved_topic": doc["title"], "format": "film", "language": "ar",
                 "audience": "Arabic-speaking adults", "editorial_intent": "نص معتمد", "research_pack": [],
                 "hard_constraints": ["No fabricated facts."]}
        class Visuals:
            events = []
            def acquire(self, plan, directory, fmt, maximum, section_estimated_seconds=None):
                directory.mkdir(parents=True, exist_ok=True)
                paths, rows = [], []
                for i in range(5):
                    path = directory/f"{i}.mp4"; path.write_bytes(b"v"*2048); paths.append(path)
                    rows.append({"section_id": f"s{i+1}", "beat_id": f"b{i+1}", "local_file": path.name,
                                 "provider": "owner_art", "asset_id": str(i)})
                return paths, rows
        def render(narration, clips, output, fmt):
            output.write_bytes(b"accepted-render"); return output
        def inspect(path):
            return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "duration_seconds": probe_duration(path.parent/"narration-mastered.wav"),
                    "width": 1920, "height": 1080, "video_streams": 1, "audio_streams": 1}
        def failed_optional(root):
            self.assertEqual(json.loads((root/"run-manifest.json").read_text())["status"], "pass")
            self.assertEqual(json.loads((root/"primary-success.json").read_text())["status"], "SUCCESS")
            raise RuntimeError("unexpected optional extractor bug")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); atomic_write_json(root/"pack.json", pack); atomic_write_json(root/"brief.json", brief)
            router = mock.Mock(events=[]); router.route.side_effect = AssertionError("model forbidden")
            with mock.patch.dict(os.environ, {owner_pack.OWNER_PACK_ENV: str(root/"pack.json")}), \
                 mock.patch("clean_v2.derived_short.attempt", side_effect=failed_optional), \
                 mock.patch("clean_v2.visual_qa.verify_final_composition_visual_qa", return_value={"status":"pass"}):
                result = CleanV2Pipeline(router=router, voice_synthesizer=LocalVoice(), visual_source=Visuals(), renderer=render,
                    final_inspector=inspect, visual_qa=_passing_visual_qa, opening_director=lambda **k: {"status":"skipped_simple_mode","slots":[]},
                    cinematic_layer=lambda **k: {"status":"pass"}, final_master_qc=_passing_final_master_qc,
                    audio_mastering=_passing_audio_mastering).run(brief_path=root/"brief.json", approved_sha256=compute_brief_sha256(brief),
                        output_dir=root/"out", engine_sha="a"*40, runner_sha="b"*40)
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["long_short_status"], "failed")
            self.assertEqual(json.loads((root/"out/run-manifest.json").read_text())["status"], "pass")
            self.assertEqual((root/"out/final.mp4").read_bytes(), b"accepted-render")

    def test_primary_SUCCESS_derivative_FAILED_delivers_primary_and_keeps_SUCCESS(self):
        direct = "https://github.com/example/repo/releases/download/tag/final.mp4"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); parent_fixture(root)
            before = (root / "final.mp4").read_bytes()
            report = derived_short.attempt(root, renderer=mock.Mock(side_effect=RuntimeError("forced derivative encoder failure")))
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["tts_calls_added"], 0)
            self.assertEqual(json.loads((root / "run-manifest.json").read_text())["status"], "pass")
            self.assertEqual(json.loads((root / "primary-success.json").read_text())["status"], "SUCCESS")
            self.assertEqual((root / "final.mp4").read_bytes(), before)
            with mock.patch.object(delivery, "send_message", return_value=True) as send:
                result = delivery.publish_one(root=root, kind="podcast", topic="", delivery_key="telegram",
                                              repository="example/repo", target_sha="a"*40, run_id="123", run_attempt="1", run=FakeRunner(direct))
            self.assertEqual(result["browser_download_url"], direct)
            self.assertEqual(send.call_args_list[0].kwargs["button_url"], direct)
            self.assertTrue(any("السبب" in c.args[0] for c in send.call_args_list))
            with zipfile.ZipFile(root / "publish-package.zip") as archive:
                package = json.loads(archive.read("package-manifest.json"))
            self.assertFalse(package["derived_short_included"])
            self.assertEqual(json.loads((root / "run-manifest.json").read_text())["status"], "pass")

    def test_invalid_span_skips_derivative_instead_of_failing_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); doc = parent_fixture(root)
            doc["derivative"]["start_text"] = "جملة غير موجودة في النص."
            atomic_write_json(root / "production.json", doc)
            report = derived_short.attempt(root)
            self.assertEqual(report["status"], "skipped")
            self.assertIn("missing", report["reason"])
            self.assertEqual(json.loads((root / "run-manifest.json").read_text())["status"], "pass")

    def test_boundary_split_preserves_words_and_the_speaker_of_continuations(self):
        original = "A: هل نبدأ اليوم؟ B: نعم، لأن الخطوة الأولى واضحة. ثم نكمل العمل بهدوء."
        units = derived_short.split_voice_units([("topic", original)], {
            "start_text": "نعم، لأن الخطوة الأولى واضحة.", "end_text": "نعم، لأن الخطوة الأولى واضحة."})
        self.assertEqual([t for _, t in units], ["A: هل نبدأ اليوم؟", "B: نعم، لأن الخطوة الأولى واضحة.", "B: ثم نكمل العمل بهدوء."])
        # Empty trailing B labels do not speak; all subsequent words retain B.
        actual = [turn for _, text in units for turn in _gemini38_dialogue_turns(text)]
        self.assertEqual([s for s, _ in actual], ["A", "B", "B"])
        self.assertEqual(" ".join(t for _, t in actual), "هل نبدأ اليوم؟ نعم، لأن الخطوة الأولى واضحة. ثم نكمل العمل بهدوء.")

    def test_podcast_rejects_an_unanswered_question_and_a_partial_turn(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); doc = parent_fixture(root)
            derived_short.freeze_primary(root); derived_short.prepare_retry_source(root)
            context = json.loads((root / "derivative-context.json").read_text())
            context["candidate"] = None
            context["rows"] = [dict(context["rows"][0], start=0, end=10)]
            selected, reason = derived_short._selection(context, doc)
            self.assertIsNone(selected)
            self.assertTrue("unanswered" in reason or "turn" in reason)

    def test_real_continuous_extract_and_retry_without_any_second_TTS(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch("clean_v2.media._gemini38_synthesize", side_effect=AssertionError("TTS forbidden")):
            root = Path(temporary); source = root / "source"; parent_fixture(source, real_media=True)
            original_hash = hashlib.sha256((source / "final.mp4").read_bytes()).hexdigest()
            report = derived_short.attempt(source)
            self.assertEqual(report["status"], "pass", report)
            self.assertAlmostEqual(report["duration_seconds"], 12, delta=0.15)
            self.assertEqual(report["clip_boundary_timing"], "exact_saved_audio_units")
            self.assertEqual(report["tts_calls_added"], 0)
            self.assertEqual(hashlib.sha256((source / "final.mp4").read_bytes()).hexdigest(), original_hash)
            ass = (source / "podcast-short.ass").read_text()
            self.assertIn("PlayResX: 1080", ass); self.assertIn("PlayResY: 1920", ass)
            self.assertIn(r"\c&H4BA2C9&", ass); self.assertIn(r"\c&HEFF3F5&", ass)
            retry = root / "retry"; retry.mkdir(); shutil.copy2(source / "final.mp4", retry / "final.mp4")
            derived_short.restore_retry_source(source / "derivative-source.zip", retry)
            self.assertFalse((retry / "audio").exists())
            again = derived_short.attempt(retry)
            self.assertEqual(again["status"], "pass", again)
            self.assertEqual(again["tts_calls_added"], 0)

    def test_extract_uses_the_caption_free_master_when_one_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); parent_fixture(root)
            (root / "final-no-captions.mp4").write_bytes(b"CLEAN")
            renderer = mock.Mock(side_effect=RuntimeError("stop after source check"))
            derived_short.attempt(root, renderer=renderer)
            self.assertEqual(renderer.call_args.args[0].name, "final-no-captions.mp4")
            (root / "final-no-captions.mp4").unlink()
            renderer = mock.Mock(side_effect=RuntimeError("stop after source check"))
            derived_short.attempt(root, renderer=renderer)
            self.assertEqual(renderer.call_args.args[0].name, "final.mp4")

    def test_retry_archive_carries_the_caption_free_master(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / "source"; parent_fixture(source)
            (source / "final-no-captions.mp4").write_bytes(b"CLEAN-MASTER")
            derived_short.freeze_primary(source); derived_short.prepare_retry_source(source)
            retry = root / "retry"; retry.mkdir(); shutil.copy2(source / "final.mp4", retry / "final.mp4")
            derived_short.restore_retry_source(source / "derivative-source.zip", retry)
            self.assertEqual((retry / "final-no-captions.mp4").read_bytes(), b"CLEAN-MASTER")
            renderer = mock.Mock(side_effect=RuntimeError("stop after source check"))
            derived_short.attempt(retry, renderer=renderer)
            self.assertEqual(renderer.call_args.args[0].name, "final-no-captions.mp4")

    def test_changed_parent_is_never_silently_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); parent_fixture(root)
            derived_short.freeze_primary(root)
            (root / "final.mp4").write_bytes(b"MUTATED")
            report = derived_short.attempt(root)
            self.assertEqual(report["status"], "failed")
            self.assertIn("changed", report["reason"])

    def test_legacy_podcast_does_not_extract_a_measured_partial_turn(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); parent_fixture(root)
            (root / "production.json").unlink()
            voice = json.loads((root / "voice-sections.json").read_text())
            text = voice["sections"][0]["chunks"][1]["text"]
            voice["sections"][0]["chunks"][1]["text"] = "B: " + text.split(". ", 1)[1]
            atomic_write_json(root / "voice-sections.json", voice)
            renderer = mock.Mock(side_effect=AssertionError("partial turn must not render"))
            report = derived_short.attempt(root, renderer=renderer)
            self.assertEqual(report["status"], "skipped", report)
            self.assertIn("turn_boundaries", report["reason"])
            renderer.assert_not_called()
            self.assertEqual(json.loads((root / "run-manifest.json").read_text())["status"], "pass")

    def test_fallback_skips_voice_chunk_cut_inside_a_sentence(self):
        doc = {"script": {"sections": [{"text": "هذه بداية جملة طويلة جدًا قبل المقطع المختار الذي ينتهي هنا."}]}}
        context = {"format": "film", "rows": [{"role": "topic", "text": "المقطع المختار الذي ينتهي هنا.",
                                                "start": 5, "end": 20, "section_id": "s2"}]}
        window, reason = derived_short._selection(context, doc)
        self.assertIsNone(window)
        self.assertEqual(reason, "no_existing_complete_7_60_second_topic_unit")

    def test_telegram_retry_dispatch_never_dispatches_primary_production(self):
        state = control.default_state(); tag = "clean-v2-final-podcast-telegram-123-a1"
        update = {"callback_query": {"data": "main:retryderivative"}}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(control, "authorized", return_value=True), \
             mock.patch.object(control, "latest_derivative_retry_tag", return_value=tag), mock.patch.object(control, "send_telegram"):
            dispatch = Path(temporary) / "dispatch.json"; control.handle_update(state, update, dispatch)
            self.assertEqual(json.loads(dispatch.read_text()), {"kind": "derivative_only", "release_tag": tag})
            self.assertEqual(state["requests"], {})
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/clean-v2-derivative-only.yml").read_text()
        self.assertNotIn("GEMINI_API_KEY", workflow); self.assertNotIn("-m clean_v2 ", workflow)


if __name__ == "__main__":
    unittest.main()
