from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from scripts import run123_planning_latency_hardening as hardening


class Run123PlanningLatencyHardeningTests(unittest.TestCase):
    def test_contract_names_distinguish_writer_doctor_dossier_and_append(self) -> None:
        cases = [
            (
                'You are writing ONE BOUNDED BATCH. Return ONLY JSON: {"sections": []} with EXACTLY 3 entries',
                "full_script",
                "script_writer_3",
            ),
            (
                'Repair ONE BOUNDED BATCH. Return ONLY JSON: {"sections": []} with EXACTLY 2 entries',
                "full_script",
                "script_doctor_2",
            ),
            (
                'Repair ONLY this bounded shard. Return ONLY JSON: {"sections": []} with EXACTLY 1 entries',
                "full_script",
                "dossier_repair_1",
            ),
            (
                'bounded residual section-length repair Return ONLY JSON: {"additions": []} with EXACTLY 7 entries',
                "append_only_repair",
                "append_repair_7",
            ),
            (
                'bounded target-completion request Return ONLY JSON: {"additions": []} with EXACTLY 1 entries',
                "append_only_repair",
                "append_repair_1",
            ),
        ]
        for prompt, base, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(hardening._contract_name_for_prompt(prompt, base), expected)

    def test_dossier_completion_budget_is_smaller_than_legacy_full_script(self) -> None:
        self.assertLess(hardening._SHARD_COMPLETION_BUDGETS["dossier_repair_1"], 2400)
        self.assertLess(hardening._SHARD_COMPLETION_BUDGETS["dossier_repair_2"], 2400)
        self.assertLess(
            hardening._SHARD_COMPLETION_BUDGETS["dossier_repair_1"],
            hardening._SHARD_COMPLETION_BUDGETS["dossier_repair_2"],
        )

    def test_append_completion_budget_tracks_target_count(self) -> None:
        self.assertEqual(hardening._SHARD_COMPLETION_BUDGETS["append_repair_1"], 600)
        self.assertLess(hardening._SHARD_COMPLETION_BUDGETS["append_repair_1"], 1800)
        self.assertLessEqual(hardening._SHARD_COMPLETION_BUDGETS["append_repair_7"], 1800)
        self.assertLess(
            hardening._SHARD_COMPLETION_BUDGETS["append_repair_1"],
            hardening._SHARD_COMPLETION_BUDGETS["append_repair_7"],
        )
        self.assertTrue(hardening._APPEND_CONTRACTS.issubset(hardening._SHARD_LOW_REASONING_CONTRACTS))

    def test_dossier_contracts_are_low_reasoning_but_keep_strict_schema(self) -> None:
        self.assertTrue(hardening._DOSSIER_CONTRACTS)
        self.assertTrue(hardening._DOSSIER_CONTRACTS.issubset(hardening._SHARD_LOW_REASONING_CONTRACTS))
        schema = {
            "type": "object",
            "properties": {"sections": {"type": "array"}},
            "required": ["sections"],
            "additionalProperties": False,
        }
        name = "dossier_repair_1"
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": name, "strict": True, "schema": schema},
        }
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])

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

    def test_append_prompts_use_compact_repair_persona_path(self) -> None:
        self.assertTrue(
            hardening._is_compact_repair_prompt(
                "This is the bounded residual section-length repair for نداء اليقظة."
            )
        )
        self.assertTrue(
            hardening._is_compact_repair_prompt(
                "This is the ONE bounded target-completion request for an append-only Film section repair."
            )
        )
        self.assertTrue(
            hardening._is_compact_repair_prompt(
                "This is ONE additional, narrowly-scoped append-only Film section request."
            )
        )

    def test_busy_groq_window_fails_over_without_sleep_or_state_loss(self) -> None:
        state = hardening.capacity._GROQ_RATE_STATE
        original = dict(state)
        try:
            state["remaining_tokens"] = 900
            state["reset_at_monotonic"] = 130.0
            with patch.object(hardening.time, "monotonic", return_value=100.0):
                with self.assertRaisesRegex(RuntimeError, "GROQ_TPM_WINDOW_BUSY_PRECHECK"):
                    hardening._fast_failover_groq_pacing({"estimated_request_tokens": 3200})
            self.assertEqual(state["remaining_tokens"], 900)
            self.assertEqual(state["reset_at_monotonic"], 130.0)
        finally:
            state.clear()
            state.update(original)

    def test_expired_groq_window_clears_local_state_and_reenables_provider(self) -> None:
        state = hardening.capacity._GROQ_RATE_STATE
        original = dict(state)
        try:
            state["remaining_tokens"] = 200
            state["reset_at_monotonic"] = 99.0
            with patch.object(hardening.time, "monotonic", return_value=100.0):
                self.assertEqual(
                    hardening._fast_failover_groq_pacing({"estimated_request_tokens": 5000}),
                    0.0,
                )
            self.assertIsNone(state["remaining_tokens"])
            self.assertIsNone(state["reset_at_monotonic"])
        finally:
            state.clear()
            state.update(original)

    def test_retry_after_cap_is_bounded_for_fast_failover(self) -> None:
        self.assertLessEqual(hardening._REPAIR_RETRY_AFTER_CAP_SECONDS, 20.0)

    def test_writer_doctor_contracts_remain_output_heavy_json_object_class(self) -> None:
        for name in hardening._WRITER_DOCTOR_CONTRACTS:
            self.assertTrue(name.startswith(("script_writer_", "script_doctor_")))
        self.assertNotIn("dossier_repair_1", hardening._WRITER_DOCTOR_CONTRACTS)
        self.assertNotIn("dossier_repair_2", hardening._WRITER_DOCTOR_CONTRACTS)
        self.assertNotIn("append_repair_1", hardening._WRITER_DOCTOR_CONTRACTS)


if __name__ == "__main__":
    unittest.main()
