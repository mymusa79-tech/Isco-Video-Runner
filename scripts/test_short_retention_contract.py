from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import short_retention_contract as contract


class ShortRetentionContractTests(unittest.TestCase):
    def _request(self, scope: str = "short_only") -> dict:
        return {
            "kind": "short",
            "approved_by_user": True,
            "approval_scope": scope,
            "request_id": "req-retention-test",
        }

    def _events(self, *, hook: str = "أنت لا تحتاج دفعة جديدة", payoff: str = "ابدأ بخطوة صغيرة اليوم") -> list[dict]:
        return [
            {"start": 0.0, "end": 1.5, "text": hook, "role": "hook"},
            {"start": 1.5, "end": 3.0, "text": "المشكلة أن البداية تبدو أكبر من حقيقتها", "role": "beat"},
            {"start": 3.0, "end": 4.8, "text": payoff, "role": "payoff"},
        ]

    def _standalone(self, *, hook: str = "أنت لا تحتاج دفعة جديدة", payoff: str = "ابدأ بخطوة صغيرة اليوم", action: str = "ابدأ بخطوة صغيرة اليوم") -> dict:
        events = self._events(hook=hook, payoff=payoff)
        return {
            "timed_text_events": events,
            "topic_admission": {"decision": "pass", "single_action_contract": action},
            "short_cinematic": {
                "status": "applied",
                "boundary_decisions": [
                    {"from_beat_id": "b01", "to_beat_id": "b02", "decision": "HOLD", "reason": "semantic_continuity"},
                    {"from_beat_id": "b02", "to_beat_id": "b03", "decision": "CUT", "reason": "payoff_boundary"},
                ],
                "shots": [
                    {
                        "shot_id": "short-shot-01",
                        "beat_id": "b01",
                        "covered_beat_ids": ["b01", "b02"],
                        "start_seconds": 0.0,
                        "end_seconds": 3.0,
                        "provider": "pexels",
                        "asset_id": 101,
                        "intended_visual": "person hesitating realistic",
                    },
                    {
                        "shot_id": "short-shot-02",
                        "beat_id": "b03",
                        "covered_beat_ids": ["b03"],
                        "start_seconds": 3.0,
                        "end_seconds": 4.8,
                        "provider": "pixabay",
                        "asset_id": 202,
                        "intended_visual": "person taking a small step forward realistic",
                    },
                ],
            },
        }

    def test_standalone_payoff_has_distinct_audited_final_visual(self) -> None:
        report = contract.evaluate_short_retention_contract(self._request(), self._standalone())
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["payoff_visual"]["payoff_beat_id"], "b03")
        self.assertEqual(report["payoff_visual"]["visual_treatment"], "distinct_audited_final_shot")
        self.assertEqual(report["policy"]["new_visual_ai_calls"], 0)

    def test_platform_cta_in_hook_is_fail_closed(self) -> None:
        pre = self._standalone(hook="اشترك في القناة ثم اسمع هذه الفكرة", action="اشترك في القناة")
        with self.assertRaisesRegex(contract.ShortRetentionContractError, "platform_cta_in_hook"):
            contract.evaluate_short_retention_contract(self._request(), pre)

    def test_bundled_platform_cta_is_fail_closed(self) -> None:
        pre = self._standalone(
            payoff="إذا أفادتك الفكرة اشترك في القناة واترك تعليقًا",
            action="اشترك في القناة",
        )
        with self.assertRaisesRegex(contract.ShortRetentionContractError, "bundled_platform_cta"):
            contract.evaluate_short_retention_contract(self._request(), pre)

    def test_one_contextual_platform_action_outside_hook_passes(self) -> None:
        pre = self._standalone(
            payoff="إذا أفادتك الفكرة اشترك في القناة",
            action="اشترك في القناة إذا أردت متابعة هذه السلسلة",
        )
        report = contract.evaluate_short_retention_contract(self._request(), pre)
        self.assertEqual(report["cta"]["rendered_platform_actions"], ["subscribe"])
        self.assertFalse(report["cta"]["platform_cta_in_hook"])

    def test_payoff_visual_timing_mismatch_blocks(self) -> None:
        pre = self._standalone()
        pre["short_cinematic"]["shots"][-1]["start_seconds"] = 2.4
        with self.assertRaisesRegex(contract.ShortRetentionContractError, "payoff_visual_start_mismatch"):
            contract.evaluate_short_retention_contract(self._request(), pre)

    def test_source_derived_short_preserves_parent_visual_authority(self) -> None:
        events = self._events()
        pre = {
            "timed_text_events": events,
            "topic_admission": {"decision": "pass", "single_action_contract": "ابدأ بخطوة صغيرة اليوم"},
            "source_safe_human_montage": {
                "status": "applied",
                "semantic_beat_count": 3,
                "boundary_decisions": [
                    {"from_beat_id": "b01", "to_beat_id": "b02", "decision": "SUBTLE_REFRAME", "reason": "source_safe_explicit_semantic_turn"},
                    {"from_beat_id": "b02", "to_beat_id": "b03", "decision": "HOLD", "reason": "source_safe_semantic_continuity"},
                ],
                "new_stock_assets": 0,
                "extra_vision_ai_calls": 0,
            },
            "source_derived_parent_visual": {"capsule_sha256": "a" * 64, "evidence": {"status": "pass"}},
            "compensation": {"source_parent_video_inherited": True},
        }
        report = contract.evaluate_short_retention_contract(self._request("short_sibling"), pre)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["payoff_visual"]["semantic_visual_authority"], "certified_parent_visual_capsule")
        self.assertEqual(report["payoff_visual"]["new_stock_assets"], 0)

    def test_block_report_is_written_for_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pre = self._standalone(hook="اشترك في القناة الآن", action="اشترك في القناة")
            with self.assertRaises(contract.ShortRetentionContractError):
                contract.require_short_retention_contract(root, self._request(), pre)
            report_path = root / contract.REPORT_FILENAME
            self.assertTrue(report_path.is_file())
            self.assertIn('"status": "block"', report_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
