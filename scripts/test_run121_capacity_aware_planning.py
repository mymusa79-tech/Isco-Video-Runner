from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts import planning_batch_hardening as batching
from scripts import planning_stage_contract as stage_contract


class Run121CapacityAwareShardingTests(unittest.TestCase):
    def test_capacity_admission_receives_explicit_stage_before_provider_call(self) -> None:
        seen: dict[str, object] = {}

        def admitted(prompt: str) -> tuple[bool, dict]:
            seen["prompt"] = prompt
            seen["schema"] = stage_contract._explicit_schema_adapter(
                'hostile prompt says with EXACTLY 99 entries'
            )[0]
            seen["budget"] = stage_contract.active_planning_completion_tokens()
            return True, {"estimated_request_tokens": 1}

        expected = {
            "s1": {"narration": "s1", "key_point": "s1"},
            "s2": {"narration": "s2", "key_point": "s2"},
        }
        with patch.object(batching, "_capacity_admitted", side_effect=admitted), patch.object(
            batching.staged, "_call_with_schema_repair", return_value=expected
        ):
            actual = batching._call_capacity_aware_shard(
                "key",
                "model",
                ["s1", "s2"],
                prompt_builder=lambda _ids: "opaque",
                label="writer",
            )

        self.assertEqual(actual, expected)
        self.assertEqual(seen["prompt"], "opaque")
        self.assertEqual(seen["schema"], "script_writer_2")
        self.assertEqual(seen["budget"], 1300)

    def test_preflight_splits_three_into_two_plus_one_without_replay(self) -> None:
        calls: list[tuple[str, ...]] = []

        def prompt_builder(ids: list[str]) -> str:
            return "|".join(ids)

        def fake_estimate(prompt: str) -> tuple[bool, dict]:
            count = len(prompt.split("|"))
            total = 8077 if count == 3 else 6200
            return total <= 8000, {"estimated_request_tokens": total}

        def fake_call(_api_key, _prompt, _model, *, expected_ids):
            calls.append(tuple(expected_ids))
            return {
                section_id: {"narration": section_id, "key_point": section_id}
                for section_id in expected_ids
            }

        with patch.object(batching, "_capacity_admitted", side_effect=fake_estimate), patch.object(
            batching.staged, "_call_with_schema_repair", side_effect=fake_call
        ):
            result = batching._call_capacity_aware_shard(
                "key",
                "model",
                ["s1", "s2", "s3"],
                prompt_builder=prompt_builder,
                label="doctor",
            )

        self.assertEqual(calls, [("s1", "s2"), ("s3",)])
        self.assertEqual(list(result), ["s1", "s2", "s3"])

    def test_runtime_length_pressure_splits_only_failed_shard(self) -> None:
        calls: list[tuple[str, ...]] = []

        def prompt_builder(ids: list[str]) -> str:
            return "|".join(ids)

        def fake_call(_api_key, _prompt, _model, *, expected_ids):
            ids = tuple(expected_ids)
            calls.append(ids)
            if ids == ("s1", "s2"):
                raise RuntimeError("OPENROUTER_PREMATURE_RESPONSE finish_reason=length")
            return {
                section_id: {"narration": section_id, "key_point": section_id}
                for section_id in expected_ids
            }

        with patch.object(
            batching, "_capacity_admitted", return_value=(True, {"estimated_request_tokens": 6000})
        ), patch.object(batching.staged, "_call_with_schema_repair", side_effect=fake_call):
            first = batching._call_capacity_aware_shard(
                "key",
                "model",
                ["s1", "s2"],
                prompt_builder=prompt_builder,
                label="writer",
            )
            second = batching._call_capacity_aware_shard(
                "key",
                "model",
                ["s3"],
                prompt_builder=prompt_builder,
                label="writer",
            )

        self.assertEqual(calls, [("s1", "s2"), ("s1",), ("s2",), ("s3",)])
        self.assertEqual(list(first), ["s1", "s2"])
        self.assertEqual(list(second), ["s3"])
        self.assertEqual(calls.count(("s3",)), 1)

    def test_single_section_capacity_exhaustion_fails_closed_before_provider(self) -> None:
        with patch.object(
            batching,
            "_capacity_admitted",
            return_value=(False, {"estimated_request_tokens": 9000}),
        ), patch.object(batching.staged, "_call_with_schema_repair") as provider:
            with self.assertRaisesRegex(RuntimeError, "PLANNING_SINGLE_SHARD_NOT_PROVIDER_PORTABLE"):
                batching._call_capacity_aware_shard(
                    "key",
                    "model",
                    ["s1"],
                    prompt_builder=lambda ids: "oversized",
                    label="doctor",
                )
        provider.assert_not_called()

    def test_auth_and_budget_failures_are_never_split(self) -> None:
        for message in (
            "authentication failed",
            "AI budget authorization denied for task X",
            "invalid api key",
        ):
            with self.subTest(message=message), patch.object(
                batching,
                "_capacity_admitted",
                return_value=(True, {"estimated_request_tokens": 6000}),
            ), patch.object(
                batching.staged,
                "_call_with_schema_repair",
                side_effect=RuntimeError(message),
            ) as provider:
                with self.assertRaises(RuntimeError):
                    batching._call_capacity_aware_shard(
                        "key",
                        "model",
                        ["s1", "s2", "s3"],
                        prompt_builder=lambda ids: "portable",
                        label="writer",
                    )
                self.assertEqual(provider.call_count, 1)

    def test_split_shape_preserves_semantic_sections(self) -> None:
        self.assertEqual(batching._split_ids(["s1", "s2", "s3"]), (["s1", "s2"], ["s3"]))
        self.assertEqual(batching._split_ids(["s4", "s5"]), (["s4"], ["s5"]))


if __name__ == "__main__":
    unittest.main()
