from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import run_control_production, short_voice_v2
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

    def test_final_mutation_happens_before_authoritative_qc_and_gold(self):
        source = inspect.getsource(run_control_production.execute_control_request)
        prepare_at = source.index("prepare_short_render(output_dir, runtime_request)")
        voice_at = source.index("apply_short_voice_v2(")
        qc_at = source.index("production.run_final_master_qc(output_dir)")
        gold_at = source.index("result = original_gold(**kwargs)")
        self.assertLess(prepare_at, voice_at)
        self.assertLess(voice_at, qc_at)
        self.assertLess(qc_at, gold_at)


if __name__ == "__main__":
    unittest.main()
