from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from scripts import dynamic_planning_capacity as dynamic
from scripts import planning_capacity_profile as profile
from scripts import planning_stage_contract as contract
from scripts import provider_capacity_hardening as capacity
from scripts import task_level_planner_router as router


class _Response:
    ok = True
    status_code = 200
    headers = {}

    def __init__(self, payload: dict):
        self._payload = payload

    def json(self):
        return self._payload


class ExplicitPlanningTransportProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        profile.install_explicit_planning_transport_projection()
        capacity.reset_groq_capacity_state_for_tests()
        router._last_call_rate_limit_headers.clear()
        router._last_call_response_meta.clear()

    def tearDown(self) -> None:
        capacity.reset_groq_capacity_state_for_tests()

    def test_every_bounded_explicit_shard_uses_canonical_budget_and_output_heavy_transport(self) -> None:
        for name, expected_budget in contract.SHARD_COMPLETION_TOKEN_BUDGETS.items():
            with self.subTest(name=name):
                self.assertEqual(
                    capacity.completion_token_budget((name, {})),
                    expected_budget,
                )
                self.assertIn(name, capacity._OUTPUT_HEAVY_CONTRACTS)
                self.assertEqual(
                    capacity._response_format_for_contract((name, {"type": "object"})),
                    {"type": "json_object"},
                )

    def test_section_repair_budget_is_projected_from_stage_contract_not_legacy_table(self) -> None:
        expected = contract.section_repair_stage_spec("s1").provider_policy.completion_tokens
        self.assertEqual(expected, 2200)
        self.assertEqual(
            capacity.completion_token_budget(("section_repair", {})),
            expected,
        )
        self.assertIn("section_repair", capacity._OUTPUT_HEAVY_CONTRACTS)

    def test_dynamic_groq_model_pool_consumes_explicit_writer_transport(self) -> None:
        captured: dict = {}
        result_payload = {
            "sections": [
                {"id": "s1", "narration": "a", "key_point": "k1"},
                {"id": "s2", "narration": "b", "key_point": "k2"},
                {"id": "s3", "narration": "c", "key_point": "k3"},
            ]
        }

        def fake_post(url, *, headers, json, timeout):
            del url, headers, timeout
            captured.update(json)
            return _Response(
                {
                    "model": "openai/gpt-oss-120b",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json_module.dumps(result_payload)
                            },
                        }
                    ],
                    "usage": {},
                }
            )

        # Alias avoids shadowing the imported json module in fake_post's keyword arg.
        json_module = json
        spec = contract.script_stage_spec("full_script", ["s1", "s2", "s3"])
        with contract.request_stage_scope(spec), \
                patch.object(capacity, "groq_admission_decision", return_value={"action": "admit", "reason": "capacity_available", "actual_limit": 8000, "remaining_tokens": 8000}), \
                patch.object(capacity, "_proactive_groq_pacing", return_value=0.0), \
                patch.object(router, "_read_secret_file", return_value="fake-key"), \
                patch.object(router.requests, "post", side_effect=fake_post):
            result = dynamic._dynamic_groq_model_call(
                "opaque explicit writer prompt",
                "openai/gpt-oss-120b",
            )

        self.assertEqual(result, result_payload)
        self.assertEqual(captured["max_completion_tokens"], 1800)
        self.assertEqual(captured["response_format"], {"type": "json_object"})
        self.assertEqual(captured["reasoning_effort"], "low")
        self.assertFalse(captured["include_reasoning"])


if __name__ == "__main__":
    unittest.main()
