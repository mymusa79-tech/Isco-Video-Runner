from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import shorts_production_binding as binding


class ShortsProductionBindingTests(unittest.TestCase):
    def _root(self, *, template: str = "inner_dialogue") -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "picture.mp4").write_bytes(b"p" * 4096)
        (root / "final.mp4").write_bytes(b"f" * 4096)
        (root / "plan.json").write_text(
            json.dumps(
                {
                    "topic": "موضوع شورت",
                    "format": "moment",
                    "hook": "لماذا تنتظر اللحظة المناسبة؟",
                    "title_options": ["اللحظة المناسبة لن تأتي"],
                    "closing_payoff": "ابدأ بخطوة واحدة الآن.",
                    "cta": "",
                    "editorial_intent": {
                        "short_template": template,
                        "short_compensation_v2": {"enabled": True, "scope": "short_only"},
                    },
                    "sections": [
                        {
                            "on_screen_text": "أنت لا تحتاج وقتًا مثاليًا.",
                            "key_point": "ابدأ الآن",
                            "visual_query": "person taking first step sunrise",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (root / "quality-final.json").write_text(
            json.dumps({"format": "moment", "video_stream_duration": 15.0}), encoding="utf-8"
        )
        return root

    def _request(self, *, authorized: bool = True, scope: str = "short_only") -> dict:
        request = {
            "request_id": "req-short",
            "request_sha256": "abc",
            "kind": "short",
            "approval_scope": scope,
            "approved_by_user": True,
            "production_dispatch_authorized": authorized,
            "short_admission": {
                "knowledge_gap_score": 8.5,
                "reframe_score": 8.0,
                "immediate_action_score": 9.0,
                "short_fit_score": 9.0,
                "single_action_contract": "اختر خطوة واحدة صغيرة وابدأ بها اليوم",
            },
        }
        if scope == "short_sibling":
            request["source_short_plan"] = {"template": "micro_story"}
        return request

    def _pre(self) -> dict:
        return {
            "short_template": "inner_dialogue",
            "compensation": {
                "profile": "short_only_compensation_v2",
                "template": "inner_dialogue",
                "beat_driven_visual_reframe_applied": True,
            },
            "topic_admission": {"decision": "pass", "single_action_contract": "ابدأ بخطوة واحدة"},
            "hook_contract": {"hook_commit_ms": 0},
            "timed_text_events": [
                {"start": 0.0, "end": 6.0, "text": "هل تنتظر؟", "role": "hook"},
                {"start": 6.0, "end": 15.0, "text": "ابدأ الآن", "role": "payoff"},
            ],
            "first_frame": {"decision": "pass"},
            "length_recommendation": {
                "band": "micro",
                "minimum_seconds": 12.0,
                "maximum_seconds": 20.0,
            },
            "progressive_text_applied": True,
        }

    def _write_final_evidence(self, root: Path, *, opening_strength: float = 0.84, narrative_progression: float = 0.82) -> None:
        for name in ("factuality-audit.json", "content-quality-audit.json", "tone-quality-audit.json"):
            (root / name).write_text(json.dumps({"status": "pass"}), encoding="utf-8")
        (root / "rights-manifest.json").write_text(
            json.dumps({"visuals": [{"source": "pexels", "id": "1"}]}), encoding="utf-8"
        )
        (root / "gold-enforce-report.json").write_text(
            json.dumps(
                {
                    "phase": "4",
                    "mode": "enforce",
                    "gold": {"accepted": True},
                    "same_render": {"artifact_divergence": False},
                }
            ),
            encoding="utf-8",
        )
        (root / "final-critic.json").write_text(
            json.dumps(
                {
                    "status": "pass",
                    "hard_blocks": [],
                    "model_review": {
                        "status": "pass",
                        "human_feel": 0.84,
                        "language_quality": 0.88,
                        "opening_strength": opening_strength,
                        "narrative_progression": narrative_progression,
                        "cultural_fit": 0.95,
                        "monetization_safety": 0.96,
                        "critical_issues": [],
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_prepare_requires_explicit_runtime_dispatch_authority(self):
        with self.assertRaisesRegex(RuntimeError, "not authorized"):
            binding.prepare_short_render(self._root(), self._request(authorized=False))

    def test_prepare_requires_topic_selected_template_for_standalone_short(self):
        root = self._root()
        plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
        plan["editorial_intent"] = {}
        (root / "plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "topic-selected template"):
            binding.prepare_short_render(root, self._request())

    def test_prepare_applies_template_rhythm_visual_compensation_and_progressive_text(self):
        root = self._root(template="inner_dialogue")

        def fake_reframe(video, events, template, output):
            self.assertEqual(template, "inner_dialogue")
            self.assertGreaterEqual(len(events), 2)
            Path(output).write_bytes(b"r" * 4096)
            return Path(output)

        def fake_render_progressive_text(*, video, events, srt_path, output):
            self.assertEqual(Path(video).name, "picture-short-comp-v2.mp4")
            Path(srt_path).write_text("stub", encoding="utf-8")
            Path(output).write_bytes(b"v" * 4096)
            return {"output": str(output)}

        def fake_remux(video, audio_source, output):
            Path(output).write_bytes(b"m" * 4096)
            return Path(output)

        with patch.object(binding, "_apply_beat_reframes", side_effect=fake_reframe) as reframe, patch.object(
            binding, "render_progressive_text", side_effect=fake_render_progressive_text
        ), patch.object(binding, "_remux_progressive_video", side_effect=fake_remux):
            context = binding.prepare_short_render(root, self._request())

        reframe.assert_called_once()
        self.assertEqual(context["short_template"], "inner_dialogue")
        self.assertEqual(context["topic_admission"]["decision"], "pass")
        self.assertLessEqual(context["hook_contract"]["hook_commit_ms"], 3000)
        self.assertGreaterEqual(len(context["timed_text_events"]), 2)
        self.assertTrue(context["progressive_text_applied"])
        self.assertTrue(context["compensation"]["beat_driven_timing_applied"])
        self.assertTrue(context["compensation"]["beat_driven_visual_reframe_applied"])
        self.assertFalse(context["compensation"]["voice_generated"])
        self.assertEqual(context["extra_ai_calls"], 0)
        self.assertTrue((root / "short-compensation-plan.json").is_file())
        self.assertEqual((root / "final.mp4").read_bytes(), b"m" * 4096)

    def test_template_changes_actual_beat_timing(self):
        texts = ["Hook", "Beat", "Payoff"]
        _hook_a, inner = binding._hook_and_text_contract(texts, 15.0, "inner_dialogue")
        _hook_b, quote = binding._hook_and_text_contract(texts, 15.0, "quote_reflection")
        self.assertNotEqual(inner[0]["end"], quote[0]["end"])
        self.assertEqual(inner[0]["template"], "inner_dialogue")
        self.assertEqual(quote[0]["template"], "quote_reflection")

    def test_sibling_short_uses_inherited_template_without_standalone_reframe(self):
        root = self._root(template="why_reframe")

        def fake_render_progressive_text(*, video, events, srt_path, output):
            self.assertEqual(Path(video).name, "picture.mp4")
            Path(srt_path).write_text("stub", encoding="utf-8")
            Path(output).write_bytes(b"v" * 4096)
            return {"output": str(output)}

        def fake_remux(video, audio_source, output):
            Path(output).write_bytes(b"m" * 4096)
            return Path(output)

        with patch.object(binding, "_apply_beat_reframes") as reframe, patch.object(
            binding, "render_progressive_text", side_effect=fake_render_progressive_text
        ), patch.object(binding, "_remux_progressive_video", side_effect=fake_remux):
            context = binding.prepare_short_render(root, self._request(scope="short_sibling"))

        reframe.assert_not_called()
        self.assertEqual(context["short_template"], "micro_story")
        self.assertFalse(context["compensation"]["beat_driven_visual_reframe_applied"])

    def test_beat_reframe_uses_template_specific_zoom_cuts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "picture.mp4"
            output = root / "reframed.mp4"
            video.write_bytes(b"v" * 2048)
            events = [
                {"start": 0.0, "end": 4.0},
                {"start": 4.0, "end": 8.0},
                {"start": 8.0, "end": 12.0},
            ]
            with patch.object(binding, "_video_dimensions", return_value=(1080, 1920)), patch(
                "scripts.shorts_production_binding.subprocess.run"
            ) as run:
                def create_output(*args, **kwargs):
                    output.write_bytes(b"x" * 4096)
                    return None
                run.side_effect = create_output
                binding._apply_beat_reframes(video, events, "micro_story", output)
            command = run.call_args.args[0]
            filter_complex = command[command.index("-filter_complex") + 1]
            self.assertIn("concat=n=3", filter_complex)
            self.assertIn("scale=1124:1996", filter_complex)
            self.assertNotIn("gemini", " ".join(command).casefold())

    def test_finalize_uses_real_final_critic_scores_and_hard_gates(self):
        root = self._root()
        self._write_final_evidence(root)
        report = binding.finalize_short_quality(root, self._request(), self._pre())
        self.assertEqual(report["quality_gate"]["decision"], "pass")
        self.assertTrue(report["delivery_allowed"])
        self.assertEqual(report["youtube_publish_mode"], "manual_in_youtube_studio")
        self.assertEqual(report["short_template"], "inner_dialogue")
        self.assertEqual(report["compensation_profile"], "short_only_compensation_v2")
        provenance = report["evidence_provenance"]
        self.assertFalse(provenance["synthetic_perfect_scores"])
        self.assertEqual(provenance["promise_payoff_score_source"], "min(opening_strength,narrative_progression)")
        self.assertAlmostEqual(report["promise_payoff"]["promise_payoff_match_score"], 8.2)
        self.assertAlmostEqual(report["identity_admission"]["channel_voice_match_score"], 8.4)

        (root / "tone-quality-audit.json").write_text(json.dumps({"status": "block"}), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "hard gates"):
            binding.finalize_short_quality(root, self._request(), self._pre())

    def test_finalize_fails_closed_when_final_critic_evidence_is_too_weak(self):
        root = self._root()
        self._write_final_evidence(root, opening_strength=0.68, narrative_progression=0.80)
        with self.assertRaises(Exception):
            binding.finalize_short_quality(root, self._request(), self._pre())

    def test_finalize_fails_closed_when_final_critic_is_missing(self):
        root = self._root()
        for name in ("factuality-audit.json", "content-quality-audit.json", "tone-quality-audit.json"):
            (root / name).write_text(json.dumps({"status": "pass"}), encoding="utf-8")
        (root / "rights-manifest.json").write_text(json.dumps({"visuals": [{}]}), encoding="utf-8")
        (root / "gold-enforce-report.json").write_text(
            json.dumps({"phase": "4", "mode": "enforce", "gold": {"accepted": True}, "same_render": {"artifact_divergence": False}}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeError, "final-critic"):
            binding.finalize_short_quality(root, self._request(), self._pre())

    def test_remux_uses_existing_audio_and_no_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "v.mp4"
            audio = root / "a.mp4"
            output = root / "out.mp4"
            video.write_bytes(b"v")
            audio.write_bytes(b"a")
            with patch("scripts.shorts_production_binding.subprocess.run") as run:
                def create_output(*args, **kwargs):
                    output.write_bytes(b"x" * 2048)
                    return None
                run.side_effect = create_output
                binding._remux_progressive_video(video, audio, output)
            command = run.call_args.args[0]
            self.assertIn("-c:v", command)
            self.assertIn("copy", command)
            self.assertNotIn("gemini", " ".join(command).casefold())


if __name__ == "__main__":
    unittest.main()
