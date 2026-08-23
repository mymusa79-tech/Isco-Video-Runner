from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import m10_live_binding as binding


class M10LiveBindingTests(unittest.TestCase):
    def _timeline(self):
        return {"final_cut_visuals": [
            {"section_id":"s1","start_seconds":0.0,"end_seconds":40.0},
            {"section_id":"s2","start_seconds":40.0,"end_seconds":80.0},
            {"section_id":"s3","start_seconds":80.0,"end_seconds":120.0},
            {"section_id":"s4","start_seconds":120.0,"end_seconds":160.0},
        ]}

    def test_only_verbatim_quote_or_stat_can_create_cards(self):
        plan = {"format":"film","sections":[
            {"id":"s1","narration":"«هذا اقتباس طويل لكنه في الافتتاح ولن يظهر إطلاقًا هنا»"},
            {"id":"s2","narration":"قال: «هذه عبارة واضحة موجودة حرفيًا داخل النص ولا يجب إعادة صياغتها»","on_screen_text":"فكرة"},
            {"id":"s3","narration":"بلغت النسبة 72٪ في هذا المثال الموثق داخل النص نفسه.","on_screen_text":"رقم"},
            {"id":"s4","narration":"لا توجد بطاقة هنا لأنها في النهاية."},
        ]}
        result = binding.plan_evidence_cards(plan, self._timeline())
        self.assertEqual(result["status"], "applied")
        self.assertEqual([x["kind"] for x in result["cards"]], ["quote","stat"])
        self.assertEqual(result["cards"][0]["primary_text"], "هذه عبارة واضحة موجودة حرفيًا داخل النص ولا يجب إعادة صياغتها")
        self.assertEqual(result["cards"][1]["primary_text"], "72٪")
        self.assertTrue(all(x["source_text_verbatim"] for x in result["cards"]))
        self.assertFalse(result["invented_claims_allowed"])

    def test_moment_and_plain_text_produce_no_cards(self):
        timeline = self._timeline()
        moment = binding.plan_evidence_cards({"format":"moment","sections":[]}, timeline)
        self.assertEqual(moment["cards"], [])
        plain = binding.plan_evidence_cards({"format":"film","sections":[{"id":"s2","narration":"فكرة عادية بلا اقتباس ولا رقم."}]}, timeline)
        self.assertEqual(plain["cards"], [])

    def test_live_mux_renders_cards_before_original_mux_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root/"plan.json").write_text(json.dumps({"format":"film","sections":[
                {"id":"s2","narration":"«هذه عبارة واضحة موجودة حرفيًا داخل النص ولا يجب إعادة صياغتها»"}
            ]}),encoding="utf-8")
            (root/"visual-timeline.json").write_text(json.dumps(self._timeline()),encoding="utf-8")
            source = root/"picture.mp4"; source.write_bytes(b"v")
            seen=[]
            def fake_render(video, request, layout, dest):
                Path(dest).write_bytes(b"card")
                return Path(dest)
            def original_mux(video,narration,output,music=None,**kwargs):
                seen.append(Path(video)); Path(output).write_bytes(b"final"); return Path(output)
            with patch.object(binding,"render_card",side_effect=fake_render), patch.object(binding.orchestrator,"mux",original_mux):
                with binding.m10_live_scope():
                    result=binding.orchestrator.mux(source,root/"n.wav",root/"final.mp4")
            self.assertEqual(result,root/"final.mp4")
            self.assertEqual(seen[0].name,"card-01.mp4")
            self.assertFalse((root/".m10").exists())
            report=json.loads((root/"m10-cards.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"],"applied")

    def test_render_failure_falls_back_to_uncarded_video_and_records_error(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/"plan.json").write_text(json.dumps({"format":"film","sections":[{"id":"s2","narration":"«هذه عبارة واضحة موجودة حرفيًا داخل النص ولا يجب إعادة صياغتها»"}]}),encoding="utf-8")
            (root/"visual-timeline.json").write_text(json.dumps(self._timeline()),encoding="utf-8")
            source=root/"picture.mp4"; source.write_bytes(b"v")
            seen=[]
            def original_mux(video,narration,output,music=None,**kwargs): seen.append(Path(video)); return Path(output)
            with patch.object(binding,"render_card",side_effect=RuntimeError("synthetic")), patch.object(binding.orchestrator,"mux",original_mux):
                with binding.m10_live_scope(): binding.orchestrator.mux(source,None,root/"final.mp4")
            self.assertEqual(seen,[source])
            report=json.loads((root/"m10-cards.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"],"render_error_fallback_to_uncarded_video")

if __name__ == "__main__": unittest.main()
