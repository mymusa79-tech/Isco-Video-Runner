from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from scripts import planning_stage_contract as stage_contract
from scripts import run123_planning_latency_hardening as hardening


MODEL = "openai/gpt-oss-120b"


class Run123PlanningLatencyHardeningTests(unittest.TestCase):
    def tearDown(self) -> None:
        hardening.capacity.reset_groq_capacity_state_for_tests()

    def test_explicit_contracts_distinguish_writer_doctor_dossier_and_append(self) -> None:
        cases = [
            (
                stage_contract.script_stage_spec("full_script", ["s1", "s2", "s3"]),
                "script_writer_3",
            ),
            (
                stage_contract.script_stage_spec("script_doctor", ["s1", "s2"]),
                "script_doctor_2",
            ),
            (
                stage_contract.script_stage_spec("dossier_repair", ["s1"]),
                "dossier_repair_1",
            ),
            (
                stage_contract.append_stage_spec(
                    [f"s{index}" for index in range(1, 8)]
                ),
                "append_repair_7",
            ),
            (
                stage_contract.append_stage_spec(["s1"]),
                "append_repair_1",
            ),
        ]
        for spec, expected in cases:
            with self.subTest(expected=expected):
                with stage_contract.request_stage_scope(spec):
                    first = stage_contract._explicit_schema_adapter(
                        'hostile text says with EXACTLY 99 entries and "additions"'
                    )
                    second = stage_contract._explicit_schema_adapter("unrelated opaque payload")
                self.assertEqual(first[0], expected)
                self.assertEqual(second[0], expected)
                self.assertEqual(first, second)
                self.assertEqual(
                    spec.provider_policy.completion_tokens,
                    hardening._SHARD_COMPLETION_BUDGETS[expected],
                )

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

    def test_busy_groq_window_fails_over_without_sleep_or_cross_model_state_loss(self) -> None:
        hardening.capacity.reset_groq_capacity_state_for_tests()
        state = hardening.capacity._model_state(MODEL)
        state["contacted"] = True
        state["actual_tpm_limit"] = 8000
        state["remaining_tokens"] = 900
        state["reset_at_epoch"] = 130.0
        other_model = "openai/gpt-oss-20b"
        other = hardening.capacity._model_state(other_model)
        other["contacted"] = True
        other["actual_tpm_limit"] = 8000
        other["remaining_tokens"] = 7000
        other["reset_at_epoch"] = 999.0

        with patch.object(hardening.capacity.time, "time", return_value=100.0):
            with self.assertRaisesRegex(RuntimeError, "GROQ_TPM_WINDOW_BUSY_PRECHECK"):
                hardening._fast_failover_groq_pacing(
                    {"estimated_request_tokens": 3200},
                    model_name=MODEL,
                )
        self.assertEqual(state["remaining_tokens"], 900)
        self.assertEqual(state["reset_at_epoch"], 130.0)
        self.assertEqual(other["remaining_tokens"], 7000)
        self.assertEqual(other["reset_at_epoch"], 999.0)

    def test_expired_groq_window_clears_only_selected_model_state(self) -> None:
        hardening.capacity.reset_groq_capacity_state_for_tests()
        state = hardening.capacity._model_state(MODEL)
        state["contacted"] = True
        state["actual_tpm_limit"] = 8000
        state["remaining_tokens"] = 200
        state["reset_at_epoch"] = 99.0
        other_model = "openai/gpt-oss-20b"
        other = hardening.capacity._model_state(other_model)
        other["contacted"] = True
        other["actual_tpm_limit"] = 8000
        other["remaining_tokens"] = 6000
        other["reset_at_epoch"] = 120.0

        with patch.object(hardening.capacity.time, "time", return_value=100.0):
            self.assertEqual(
                hardening._fast_failover_groq_pacing(
                    {"estimated_request_tokens": 5000},
                    model_name=MODEL,
                ),
                0.0,
            )
        self.assertIsNone(state["remaining_tokens"])
        self.assertIsNone(state["reset_at_epoch"])
        self.assertEqual(other["remaining_tokens"], 6000)
        self.assertEqual(other["reset_at_epoch"], 120.0)

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
