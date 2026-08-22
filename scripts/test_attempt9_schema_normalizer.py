from __future__ import annotations

import unittest
from unittest.mock import patch

import isco_video_agent.resilient_planner as staged
import scripts.append_retry_guard as append_guard
from scripts.attempt9_schema_normalizer import (
    _normalize_additions_payload,
    install_attempt9_schema_normalizer,
)


class Attempt9SchemaNormalizerUnitTests(unittest.TestCase):
    def test_canonical_payload_is_preserved(self) -> None:
        payload = {"additions": [{"id": "s1", "append_text": "نص آمن"}]}
        self.assertIs(_normalize_additions_payload(payload, ["s1"]), payload)

    def test_top_level_list_is_normalized(self) -> None:
        payload = [{"id": "s1", "append_text": "نص أول"}]
        normalized = _normalize_additions_payload(payload, ["s1"])
        self.assertEqual(normalized, {"additions": payload})

    def test_single_addition_object_is_wrapped(self) -> None:
        normalized = _normalize_additions_payload(
            {"additions": {"id": "s1", "append_text": "نص أول"}},
            ["s1", "s2"],
        )
        self.assertEqual(
            normalized,
            {"additions": [{"id": "s1", "append_text": "نص أول"}]},
        )

    def test_id_keyed_mapping_is_normalized_in_contract_order(self) -> None:
        normalized = _normalize_additions_payload(
            {"additions": {"s2": "الثاني", "s1": "الأول"}},
            ["s1", "s2"],
        )
        self.assertEqual(
            normalized,
            {
                "additions": [
                    {"id": "s1", "append_text": "الأول"},
                    {"id": "s2", "append_text": "الثاني"},
                ]
            },
        )

    def test_nested_id_mapping_must_not_contradict_its_key(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "id mismatch"):
            _normalize_additions_payload(
                {
                    "additions": {
                        "s1": {"id": "s2", "append_text": "لا يطبق"},
                    }
                },
                ["s1", "s2"],
            )

    def test_unknown_id_and_ambiguous_schema_fail_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unexpected section id: attacker"):
            _normalize_additions_payload(
                {"additions": {"attacker": "لا يطبق"}},
                ["s1"],
            )
        with self.assertRaisesRegex(RuntimeError, "unsupported schema type"):
            _normalize_additions_payload({"additions": "not-a-list"}, ["s1"])
        with self.assertRaisesRegex(RuntimeError, "unexpected section id: data"):
            _normalize_additions_payload({"data": {"additions": []}}, ["s1"])

    def test_diagnostics_never_print_append_text(self) -> None:
        secret_text = "SENSITIVE-NARRATION-MUST-NOT-LOG"
        with patch("builtins.print") as mocked_print:
            _normalize_additions_payload(
                [{"id": "s1", "append_text": secret_text}],
                ["s1"],
            )
        rendered = " ".join(str(call) for call in mocked_print.call_args_list)
        self.assertNotIn(secret_text, rendered)
        self.assertIn("s1", rendered)


class Attempt9SchemaNormalizerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_subset = append_guard._parse_ordered_subset_for_schema_completion
        self.original_complete = append_guard._parse_safe_partial_additions
        self.original_engine_parser = staged._parse_append_only_response

    def tearDown(self) -> None:
        append_guard._parse_ordered_subset_for_schema_completion = self.original_subset
        append_guard._parse_safe_partial_additions = self.original_complete
        staged._parse_append_only_response = self.original_engine_parser

    def test_installer_keeps_existing_semantic_order_gate(self) -> None:
        install_attempt9_schema_normalizer()
        with self.assertRaisesRegex(RuntimeError, "preserve the required section-id order"):
            append_guard._parse_ordered_subset_for_schema_completion(
                [
                    {"id": "s2", "append_text": "الثاني"},
                    {"id": "s1", "append_text": "الأول"},
                ],
                ["s1", "s2"],
            )

    def test_complete_parser_still_requires_every_expected_target(self) -> None:
        install_attempt9_schema_normalizer()
        with self.assertRaisesRegex(RuntimeError, "exactly 2 additions"):
            append_guard._parse_safe_partial_additions(
                {"additions": {"id": "s1", "append_text": "واحد"}},
                ["s1", "s2"],
            )

    def test_attempt9_equivalent_shape_does_not_add_provider_calls(self) -> None:
        sections = [
            staged.ScriptSection(
                id=f"sec_{index}",
                narration=" ".join([f"كلمة{index}"] * 100),
                visual_query="room notebook",
                key_point=f"distinct key point {index}",
            )
            for index in range(1, 8)
        ]
        sections.append(
            staged.ScriptSection(
                id="sec_8",
                narration=" ".join(["كلمة8"] * 70),
                visual_query="room notebook",
                key_point="distinct key point 8",
            )
        )
        # 770 words: below the 800 aggregate floor, with all eight sections below
        # the 110-word section floor. A canonical-equivalent top-level list should be
        # accepted in the original provider call, not consume a schema-repair call.
        calls = 0

        def fake_json(api_key, prompt, model):
            nonlocal calls
            del api_key, prompt, model
            calls += 1
            return [
                {
                    "id": section.id,
                    "append_text": " ".join(
                        ["إضافة"] * (110 - staged._word_count(section.narration) + 6)
                    ),
                }
                for section in sections
            ]

        install_attempt9_schema_normalizer()
        with patch.object(staged, "json_text", side_effect=fake_json):
            additions = append_guard._repair_all_residual_underlength(
                "key",
                topic="topic",
                model="model",
                sections=sections,
                policy_json="{}",
                research_json="{}",
                narrative_format="problem_reveal_solution",
                current_words=770,
                minimum=800,
            )
        self.assertEqual(calls, 1)
        self.assertEqual(list(additions), [f"sec_{index}" for index in range(1, 9)])
        self.assertTrue(all(staged._word_count(text) >= 16 for text in additions.values()))


if __name__ == "__main__":
    unittest.main()
