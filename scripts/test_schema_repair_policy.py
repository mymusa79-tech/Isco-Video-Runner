from __future__ import annotations

import unittest
from unittest.mock import patch

import isco_video_agent.resilient_planner as staged
from scripts.schema_repair_policy import install_schema_repair_policy


class SchemaRepairPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = staged._call_with_schema_repair

    def tearDown(self) -> None:
        staged._call_with_schema_repair = self.original

    @staticmethod
    def _valid() -> dict:
        return {
            "sections": [
                {"id": "s1", "narration": "one", "key_point": "k1"},
                {"id": "s2", "narration": "two", "key_point": "k2"},
            ]
        }

    def test_provider_failure_is_not_retried_as_schema_failure(self) -> None:
        install_schema_repair_policy()
        calls = 0

        def fake_json(api_key, prompt, model):
            nonlocal calls
            del api_key, prompt, model
            calls += 1
            raise RuntimeError("All free providers failed for planning subtask: timeout")

        with patch.object(staged, "json_text", side_effect=fake_json):
            with self.assertRaisesRegex(RuntimeError, "All free providers failed"):
                staged._call_with_schema_repair(
                    "key",
                    "prompt",
                    "model",
                    expected_ids=["s1", "s2"],
                )
        self.assertEqual(calls, 1)

    def test_local_shape_failure_gets_one_schema_reask(self) -> None:
        install_schema_repair_policy()
        calls: list[str] = []

        def fake_json(api_key, prompt, model):
            del api_key, model
            calls.append(prompt)
            if len(calls) == 1:
                return {"sections": [{"id": "s1", "narration": "one", "key_point": "k1"}]}
            return self._valid()

        with patch.object(staged, "json_text", side_effect=fake_json):
            result = staged._call_with_schema_repair(
                "key",
                "prompt",
                "model",
                expected_ids=["s1", "s2"],
            )
        self.assertEqual(len(calls), 2)
        self.assertIn("previous response was not valid", calls[1])
        self.assertEqual(list(result), ["s1", "s2"])

    def test_second_shape_failure_fails_closed_without_third_call(self) -> None:
        install_schema_repair_policy()
        calls = 0

        def fake_json(api_key, prompt, model):
            nonlocal calls
            del api_key, prompt, model
            calls += 1
            return {"sections": [{"id": "s1", "narration": "one", "key_point": "k1"}]}

        with patch.object(staged, "json_text", side_effect=fake_json):
            with self.assertRaisesRegex(RuntimeError, "must contain exactly 2 sections"):
                staged._call_with_schema_repair(
                    "key",
                    "prompt",
                    "model",
                    expected_ids=["s1", "s2"],
                )
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
