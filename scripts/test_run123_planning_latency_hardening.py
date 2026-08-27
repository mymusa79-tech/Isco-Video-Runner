from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from scripts import run123_planning_latency_hardening as hardening


class Run123PlanningLatencyHardeningTests(unittest.TestCase):
    def test_contract_names_distinguish_writer_doctor_and_dossier(self) -> None:
        cases = [
            (
                'You are writing ONE BOUNDED BATCH. Return ONLY JSON: {"sections": []} with EXACTLY 3 entries',
                "script_writer_3",
            ),
            (
                'Repair ONE BOUNDED BATCH. Return ONLY JSON: {"sections": []} with EXACTLY 2 entries',
                "script_doctor_2",
            ),
            (
                'Repair ONLY this bounded shard. Return ONLY JSON: {"sections": []} with EXACTLY 1 entries',
                "dossier_repair_1",
            ),
        ]
        for prompt, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    hardening._contract_name_for_prompt(prompt, "full_script"), expected
                )

    def test_dossier_completion_budget_is_smaller_than_legacy_full_script(self) -> None:
        self.assertLess(hardening._SHARD_COMPLETION_BUDGETS["dossier_repair_1"], 2400)
        self.assertLess(hardening._SHARD_COMPLETION_BUDGETS["dossier_repair_2"], 2400)
        self.assertLess(
            hardening._SHARD_COMPLETION_BUDGETS["dossier_repair_1"],
            hardening._SHARD_COMPLETION_BUDGETS["dossier_repair_2"],
        )

    def test_compact_research_preserves_claim_scope_and_removes_transport_url(self) -> None:
        raw = json.dumps(
            {
                "approved_research_pack": [
                    {
                        "source_title": "Study",
                        "source_url": "https://example.com/study",
                        "claim_scope": "Only support the bounded claim.",
                    }
                ],
                "content_boundaries": ["No fabricated statistics"],
                "factuality_rule": "Use approved evidence only",
            }
        )
        compact = json.loads(hardening.compact_planning_research_json(raw))
        self.assertEqual(
            compact["approved_research_pack"][0]["claim_scope"],
            "Only support the bounded claim.",
        )
        self.assertNotIn("source_url", compact["approved_research_pack"][0])
        self.assertEqual(compact["content_boundaries"], ["No fabricated statistics"])
        self.assertEqual(compact["factuality_rule"], "Use approved evidence only")

    def test_compact_text_policy_keeps_narration_rules_but_drops_media_only_payload(self) -> None:
        raw = json.dumps(
            {
                "version": 1,
                "language": {"register": "MSA"},
                "values": {"rules": ["respect"]},
                "brand_signature": {"host_managed": True},
                "release_gate": {"reject_on_obvious_ai_feel": True},
                "visuals": {"rules": ["visual-only"]},
                "audio": {"rules": ["audio-only"]},
            }
        )
        compact = json.loads(hardening.compact_text_policy_json(raw))
        self.assertIn("language", compact)
        self.assertIn("values", compact)
        self.assertIn("brand_signature", compact)
        self.assertIn("release_gate", compact)
        self.assertNotIn("visuals", compact)
        self.assertNotIn("audio", compact)

    def test_compact_repair_persona_keeps_core_voice_contract(self) -> None:
        persona = {
            "version": 1,
            "channel": "نداء اليقظة",
            "writing_voice": {
                "tone": "tone",
                "cadence": ["cadence"],
                "signature_moves": ["move"],
                "banned_ai_phrases": ["ban"],
            },
            "analysis_lens": {
                "principle": "principle",
                "required_moves": ["required"],
                "generic_rejection_rule": "reject",
            },
            "dialogue_contract": {"rule": "dialogue", "question_answer_rule": "single"},
        }
        with patch.object(hardening, "load_channel_persona", return_value=persona):
            enriched = hardening._compact_repair_persona(
                "Repair ONE BOUNDED BATCH for نداء اليقظة dialogue_qa"
            )
        self.assertIn("<CHANNEL_PERSONA>", enriched)
        self.assertIn("tone", enriched)
        self.assertIn("cadence", enriched)
        self.assertIn("move", enriched)
        self.assertIn("ban", enriched)
        self.assertIn("principle", enriched)
        self.assertIn("required", enriched)
        self.assertIn("dialogue", enriched)

    def test_retry_after_cap_is_bounded_for_fast_failover(self) -> None:
        self.assertLessEqual(hardening._REPAIR_RETRY_AFTER_CAP_SECONDS, 20.0)

    def test_writer_doctor_contracts_remain_output_heavy_json_object_class(self) -> None:
        for name in hardening._WRITER_DOCTOR_CONTRACTS:
            self.assertTrue(name.startswith(("script_writer_", "script_doctor_")))
        self.assertNotIn("dossier_repair_1", hardening._WRITER_DOCTOR_CONTRACTS)
        self.assertNotIn("dossier_repair_2", hardening._WRITER_DOCTOR_CONTRACTS)


if __name__ == "__main__":
    unittest.main()
