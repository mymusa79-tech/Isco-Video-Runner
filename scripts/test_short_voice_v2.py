from __future__ import annotations

import inspect
import unittest

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

    def test_source_derived_short_is_not_revoiced_by_standalone_v2(self):
        pre = {"short_template": "micro_story", "timed_text_events": [{"text": "أ"}, {"text": "ب"}]}
        result = apply_short_voice_v2(
            ".",
            {"approval_scope": "short_sibling"},
            pre,
            ledger=object(),
        )
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
