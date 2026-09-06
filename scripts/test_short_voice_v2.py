from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import orchestration_shorts_port, run_control_production, short_voice_v2
from scripts.short_voice_v2 import _voice_script, apply_short_voice_v2, decide_voice_mode


class ShortVoiceV2Tests(unittest.TestCase):
    def test_template_drives_voice_mode(self):
        self.assertEqual(decide_voice_mode("inner_dialogue"), "voice_led")
        self.assertEqual(decide_voice_mode("micro_story"), "voice_led")
        self.assertEqual(decide_voice_mode("why_reframe"), "hybrid")
        self.assertEqual(decide_voice_mode("quote_reflection"), "hybrid")
        with self.assertRaisesRegex(RuntimeError, "unsupported template"):
            decide_voice_mode("unknown")

    def test_voice_led_reads_semantic_progression_while_hybrid_uses_hook_and_payoff(self):
        events = [
            {"text": "الخطوة الأولى"},
            {"text": "ثم يظهر الاحتكاك"},
            {"text": "وهنا يتغير المنظور"},
            {"text": "ابدأ بحركة صغيرة"},
        ]
        led = _voice_script(events, "voice_led")
        hybrid = _voice_script(events, "hybrid")
        self.assertIn("ثم يظهر الاحتكاك", led)
        self.assertIn("وهنا يتغير المنظور", led)
        self.assertNotIn("ثم يظهر الاحتكاك", hybrid)
        self.assertTrue(hybrid.startswith("الخطوة الأولى"))
        self.assertIn("ابدأ بحركة صغيرة", hybrid)

    def _assert_scope_is_voiced(self, scope: str) -> dict:
        """Keep direct legacy Voice V2 coverage while the authoritative seam moves on."""
        pre = {
            "short_template": "micro_story",
            "timed_text_events": [
                {"text": "المشهد يبدأ هنا"},
                {"text": "ثم يتغير المعنى"},
            ],
            "compensation": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "final.mp4").write_bytes(b"short")
            with (
                mock.patch.object(short_voice_v2, "secret", return_value="key"),
                mock.patch.object(short_voice_v2, "env", side_effect=lambda _name, default=None: default),
                mock.patch.object(short_voice_v2.orchestrator, "_synthesize_tts_section"),
                mock.patch.object(short_voice_v2, "consume_voice_provenance", return_value={"provider": "piper", "fallback_used": True}),
                mock.patch.object(short_voice_v2, "_final_duration", return_value=15.0),
                mock.patch.object(short_voice_v2, "_fit_voice_to_video", return_value=root / "voice.wav"),
                mock.patch.object(short_voice_v2, "_mix_voice"),
                mock.patch.object(short_voice_v2.shutil, "move"),
                mock.patch.object(short_voice_v2, "_refresh_quality_final", return_value={"quality_measurement_stage": "post_short_voice_pre_gold"}),
                mock.patch.object(short_voice_v2, "_record_voice_rights"),
            ):
                result = apply_short_voice_v2(root, {"approval_scope": scope}, pre, ledger=object())
        self.assertTrue(result["compensation"]["voice_generated"])
        self.assertEqual(result["compensation"]["voice_scope"], scope)
        self.assertEqual(result["voice"]["scope"], scope)
        return result

    def test_standalone_short_is_voiced(self):
        result = self._assert_scope_is_voiced("short_only")
        self.assertFalse(result["voice"]["source_derived_from_long"])

    def test_source_derived_short_is_voiced_from_inherited_template(self):
        result = self._assert_scope_is_voiced("short_sibling")
        self.assertTrue(result["voice"]["source_derived_from_long"])
        self.assertEqual(result["voice"]["template"], "micro_story")
        self.assertEqual(result["voice"]["mode"], "voice_led")

    def _voice_led_pre(self) -> dict:
        return {
            "short_template": "inner_dialogue",
            "timed_text_events": [
                {"text": "سؤال قصير"},
                {"text": "خطوة صغيرة"},
                {"text": " ".join(["تفصيل"] * 18)},
                {"text": "ابدأ الآن"},
            ],
            "compensation": {},
        }

    def test_dense_narration_retries_once_and_succeeds_with_a_smaller_projection(self):
        # Run #196: the real post-synthesis speed can exceed RUNTIME_MAX_SPEED even
        # though build_voice_projection()'s word-rate estimate fit the padded
        # planning budget. The bounded recovery re-projects to a smaller candidate
        # and re-synthesizes exactly once before giving up.
        pre = self._voice_led_pre()
        synth_calls: list[str] = []

        def fake_synthesize(_ledger, _circuit, _budget, *, task_id, **_kwargs):
            synth_calls.append(task_id)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "final.mp4").write_bytes(b"short")
            with (
                mock.patch.object(short_voice_v2, "secret", return_value="key"),
                mock.patch.object(short_voice_v2, "env", side_effect=lambda _name, default=None: default),
                mock.patch.object(short_voice_v2.orchestrator, "_synthesize_tts_section", side_effect=fake_synthesize),
                mock.patch.object(short_voice_v2, "consume_voice_provenance", return_value={"provider": "gemini", "fallback_used": False}),
                mock.patch.object(short_voice_v2, "_final_duration", return_value=15.0),
                mock.patch.object(
                    short_voice_v2, "_fit_voice_to_video",
                    side_effect=[RuntimeError("... too dense for the approved duration: speed_required=1.280"), root / "voice.wav"],
                ),
                mock.patch.object(short_voice_v2, "_mix_voice"),
                mock.patch.object(short_voice_v2.shutil, "move"),
                mock.patch.object(short_voice_v2, "_refresh_quality_final", return_value={"quality_measurement_stage": "post_short_voice_pre_gold"}),
                mock.patch.object(short_voice_v2, "_record_voice_rights"),
            ):
                result = apply_short_voice_v2(root, {"approval_scope": "short_only"}, pre, ledger=object())

        self.assertEqual(synth_calls, ["SHORT_VOICE_V2", "SHORT_VOICE_V2_RETRY"])
        self.assertTrue(result["compensation"]["voice_dense_retry_used"])
        self.assertEqual(result["compensation"]["voice_task_id"], "SHORT_VOICE_V2_RETRY")
        self.assertLess(
            result["compensation"]["voice_spoken_beat_count"],
            result["compensation"]["voice_original_beat_count"],
        )

    def test_non_density_fit_failure_is_not_retried(self):
        # A _fit_voice_to_video failure unrelated to narration density (e.g. a
        # corrupt/unreadable file) must propagate immediately - the bounded retry
        # only ever engages for the exact "too dense" signature.
        pre = self._voice_led_pre()
        synth_calls: list[str] = []

        def fake_synthesize(_ledger, _circuit, _budget, *, task_id, **_kwargs):
            synth_calls.append(task_id)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "final.mp4").write_bytes(b"short")
            with (
                mock.patch.object(short_voice_v2, "secret", return_value="key"),
                mock.patch.object(short_voice_v2, "env", side_effect=lambda _name, default=None: default),
                mock.patch.object(short_voice_v2.orchestrator, "_synthesize_tts_section", side_effect=fake_synthesize),
                mock.patch.object(short_voice_v2, "consume_voice_provenance", return_value={"provider": "gemini", "fallback_used": False}),
                mock.patch.object(short_voice_v2, "_final_duration", return_value=15.0),
                mock.patch.object(
                    short_voice_v2, "_fit_voice_to_video",
                    side_effect=RuntimeError("Short Voice V2 cannot resolve final duration"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "cannot resolve final duration"):
                    apply_short_voice_v2(root, {"approval_scope": "short_only"}, pre, ledger=object())

        self.assertEqual(synth_calls, ["SHORT_VOICE_V2"])

    def test_dense_narration_retry_exhausted_raises_impossible_when_no_smaller_projection(self):
        # Hybrid mode has only one candidate (hook+payoff); if that one comes back
        # too dense at the real measurement there is nothing smaller left to try, so
        # this must fail closed with the "impossible without rewriting" signature
        # rather than looping or silently accepting an over-speed narration.
        pre = {
            "short_template": "why_reframe",
            "timed_text_events": [
                {"text": "هذا هو السؤال"},
                {"text": "تفصيل داخلي"},
                {"text": "وهذه هي الخلاصة"},
            ],
            "compensation": {},
        }
        synth_calls: list[str] = []

        def fake_synthesize(_ledger, _circuit, _budget, *, task_id, **_kwargs):
            synth_calls.append(task_id)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "final.mp4").write_bytes(b"short")
            with (
                mock.patch.object(short_voice_v2, "secret", return_value="key"),
                mock.patch.object(short_voice_v2, "env", side_effect=lambda _name, default=None: default),
                mock.patch.object(short_voice_v2.orchestrator, "_synthesize_tts_section", side_effect=fake_synthesize),
                mock.patch.object(short_voice_v2, "consume_voice_provenance", return_value={"provider": "gemini", "fallback_used": False}),
                mock.patch.object(short_voice_v2, "_final_duration", return_value=15.0),
                mock.patch.object(
                    short_voice_v2, "_fit_voice_to_video",
                    side_effect=RuntimeError("... too dense for the approved duration: speed_required=1.280"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "impossible without rewriting"):
                    apply_short_voice_v2(root, {"approval_scope": "short_only"}, pre, ledger=object())

        # Exactly one real TTS call was spent - never a second one when no smaller
        # projection exists to justify it.
        self.assertEqual(synth_calls, ["SHORT_VOICE_V2"])

    def test_non_short_scope_is_not_voiced(self):
        pre = {"short_template": "micro_story", "timed_text_events": [{"text": "أ"}, {"text": "ب"}]}
        result = apply_short_voice_v2(".", {"approval_scope": "long_only"}, pre, ledger=object())
        self.assertIs(result, pre)

    def test_voice_mutation_refreshes_quality_and_rights_before_return(self):
        source = inspect.getsource(short_voice_v2.apply_short_voice_v2)
        mix_at = source.index("_mix_voice(")
        move_at = source.index("shutil.move(")
        quality_at = source.index("_refresh_quality_final(")
        rights_at = source.index("_record_voice_rights(")
        self.assertLess(mix_at, move_at)
        self.assertLess(move_at, quality_at)
        self.assertLess(quality_at, rights_at)
        self.assertIn('"quality_final_refreshed_after_voice": True', source)

    def test_authoritative_seam_uses_voice_owned_timeline_before_qc_and_gold(self):
        seam = inspect.getsource(orchestration_shorts_port.prepare_authoritative_short_for_gold)
        prepare_at = seam.index("core.prepare_short_render(")
        voice_at = seam.index("apply_voice_owned_short(")
        qc_at = seam.index("run_final_master_qc(output_dir)")
        self.assertLess(prepare_at, voice_at)
        self.assertLess(voice_at, qc_at)
        self.assertNotIn("apply_short_voice_v2(", seam)

        source = inspect.getsource(run_control_production.execute_control_request)
        seam_at = source.index("prepare_authoritative_short_for_gold(")
        gold_at = source.index("result = original_gold(**kwargs)")
        self.assertLess(seam_at, gold_at)


if __name__ == "__main__":
    unittest.main()
