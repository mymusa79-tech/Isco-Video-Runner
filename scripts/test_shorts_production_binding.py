from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import shorts_production_binding as binding


class ShortsProductionBindingTests(unittest.TestCase):
    def _root(self) -> Path:
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

    def _request(self, *, authorized: bool = True) -> dict:
        return {
            "request_id": "req-short",
            "request_sha256": "abc",
            "kind": "short",
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

    def _pre(self) -> dict:
        return {
            "topic_admission": {"decision": "pass", "single_action_contract": "ابدأ بخطوة واحدة"},
            "hook_contract": {"hook_commit_ms": 0},
            "timed_text_events": [
                {"start": 0.0, "end": 7.5, "text": "هل تنتظر؟", "role": "hook"},
                {"start": 7.5, "end": 15.0, "text": "ابدأ الآن", "role": "payoff"},
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

    def test_prepare_validates_hook_topic_length_and_progressive_text(self):
        root = self._root()

        def fake_render_progressive_text(*, video, events, srt_path, output):
            Path(srt_path).write_text("stub", encoding="utf-8")
            Path(output).write_bytes(b"v" * 4096)
            return {"output": str(output)}

        def fake_remux(video, audio_source, output):
            Path(output).write_bytes(b"m" * 4096)
            return Path(output)

        with patch.object(binding, "render_progressive_text", side_effect=fake_render_progressive_text), patch.object(
            binding, "_remux_progressive_video", side_effect=fake_remux
        ):
            context = binding.prepare_short_render(root, self._request())

        self.assertEqual(context["topic_admission"]["decision"], "pass")
        self.assertLessEqual(context["hook_contract"]["hook_commit_ms"], 3000)
        self.assertGreaterEqual(len(context["timed_text_events"]), 2)
        self.assertTrue(context["progressive_text_applied"])
        self.assertEqual(context["extra_ai_calls"], 0)
        self.assertEqual((root / "final.mp4").read_bytes(), b"m" * 4096)

    def test_finalize_uses_real_final_critic_scores_and_hard_gates(self):
        root = self._root()
        self._write_final_evidence(root)
        report = binding.finalize_short_quality(root, self._request(), self._pre())
        self.assertEqual(report["quality_gate"]["decision"], "pass")
        self.assertTrue(report["delivery_allowed"])
        self.assertEqual(report["youtube_publish_mode"], "manual_in_youtube_studio")
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
