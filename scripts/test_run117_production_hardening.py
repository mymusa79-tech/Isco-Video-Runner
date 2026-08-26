from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import isco_video_agent.resilient_planner as staged

from scripts import planning_batch_hardening as batching
from scripts import provider_capacity_hardening as capacity
from scripts.provider_failure import classify_provider_failure


class Run117FailureClassificationTests(unittest.TestCase):
    def test_413_rate_limit_wording_is_request_scoped_not_provider_quota(self) -> None:
        failure = classify_provider_failure(
            "groq",
            "GROQ_HTTP_413 status=413 code=rate_limit_exceeded "
            "message=Request too large for model on tokens per minute (TPM): Limit 8000, Requested 9200",
        )
        self.assertEqual(failure.telemetry_result, "payload_too_large")
        self.assertFalse(failure.open_circuit)

    def test_local_tpm_capacity_preflight_is_request_scoped(self) -> None:
        failure = classify_provider_failure(
            "groq",
            "GROQ_TPM_CAPACITY_PREFLIGHT estimated_total=8400 limit=8000",
        )
        self.assertEqual(failure.telemetry_result, "payload_too_large")
        self.assertFalse(failure.open_circuit)


class ProviderCapacityPolicyTests(unittest.TestCase):
    @staticmethod
    def _full_script_prompt(payload: str) -> str:
        return (
            'Return ONLY JSON: {"sections": []} with EXACTLY 3 entries\n'
            + payload
        )

    def setUp(self) -> None:
        capacity._GROQ_RATE_STATE["remaining_tokens"] = None
        capacity._GROQ_RATE_STATE["reset_at_monotonic"] = None

    def test_bounded_full_script_reserve_is_smaller_than_run117_whole_script_reserve(self) -> None:
        contract = capacity.router._structured_schema_for_prompt(self._full_script_prompt("x"))
        self.assertIsNotNone(contract)
        self.assertEqual(contract[0], "full_script")
        self.assertEqual(capacity.completion_token_budget(contract), 2400)

    def test_capacity_estimate_accepts_normal_batch_but_rejects_oversized_request(self) -> None:
        normal = capacity.groq_capacity_estimate(self._full_script_prompt("x" * 12_000))
        oversized = capacity.groq_capacity_estimate(self._full_script_prompt("x" * 30_000))
        self.assertLessEqual(normal["estimated_request_tokens"], capacity.GROQ_FREE_TPM_LIMIT)
        self.assertGreater(oversized["estimated_request_tokens"], capacity.GROQ_FREE_TPM_LIMIT)

    def test_retry_after_bound_can_honor_run117_69_second_header(self) -> None:
        self.assertGreaterEqual(capacity.MAX_RETRY_AFTER_SECONDS, 69.0)
        self.assertLessEqual(capacity.MAX_RETRY_AFTER_SECONDS, 120.0)

    def test_groq_reset_header_parser_supports_documented_compound_duration(self) -> None:
        self.assertAlmostEqual(capacity._duration_header_seconds("2m59.56s"), 179.56)
        self.assertAlmostEqual(capacity._duration_header_seconds("7.66s"), 7.66)
        self.assertIsNone(capacity._duration_header_seconds("not-a-duration"))

    def test_run119_low_remaining_tokens_waits_for_reset_before_provider_attempt(self) -> None:
        # Run #119 repeatedly needed ~7.7K tokens while Groq reported only tens/hundreds
        # remaining. The reset-aware admission wait must happen before another request.
        headers = {
            "x-ratelimit-remaining-tokens": "112",
            "x-ratelimit-reset-tokens": "1.0s",
        }
        with patch.object(capacity.time, "monotonic", side_effect=[100.0, 100.25]), \
                patch.object(capacity.time, "sleep") as sleep_mock:
            capacity._update_groq_rate_state(headers)
            waited = capacity._proactive_groq_pacing({"estimated_request_tokens": 7749})

        self.assertAlmostEqual(waited, 2.25)
        sleep_mock.assert_called_once_with(waited)
        self.assertIsNone(capacity._GROQ_RATE_STATE["remaining_tokens"])
        self.assertIsNone(capacity._GROQ_RATE_STATE["reset_at_monotonic"])

    def test_groq_pacing_does_not_wait_when_remaining_tokens_cover_request(self) -> None:
        capacity._GROQ_RATE_STATE["remaining_tokens"] = 7800
        capacity._GROQ_RATE_STATE["reset_at_monotonic"] = 999.0
        with patch.object(capacity.time, "sleep") as sleep_mock:
            waited = capacity._proactive_groq_pacing({"estimated_request_tokens": 7749})
        self.assertEqual(waited, 0.0)
        sleep_mock.assert_not_called()

    def test_openrouter_full_script_uses_low_reasoning_and_free_model_failover(self) -> None:
        prompt = self._full_script_prompt("write")
        contract = capacity.router._structured_schema_for_prompt(prompt)
        captured: dict = {}

        class FakeResponse:
            ok = True
            status_code = 200
            headers = {}

            def json(self):
                return {
                    "model": "nvidia/nemotron-3-super-120b-a12b:free",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(
                                    {
                                        "sections": [
                                            {"id": "s1", "narration": "a", "key_point": "k1"},
                                            {"id": "s2", "narration": "b", "key_point": "k2"},
                                            {"id": "s3", "narration": "c", "key_point": "k3"},
                                        ]
                                    }
                                )
                            },
                        }
                    ],
                    "usage": {},
                }

        def fake_post(url, *, headers, json, timeout):
            del url, headers, timeout
            captured.update(json)
            return FakeResponse()

        with patch.object(capacity.router, "_openrouter_key", return_value="fake"), \
                patch.object(capacity.router.requests, "post", side_effect=fake_post):
            result = capacity._hardened_openrouter_structured_request(prompt, contract)

        self.assertIn("sections", result)
        self.assertEqual(captured["models"], list(capacity.OPENROUTER_OUTPUT_HEAVY_MODELS))
        self.assertGreaterEqual(len(captured["models"]), 3)
        self.assertEqual(captured["models"][-1], "openrouter/free")
        self.assertTrue(
            all(model == "openrouter/free" or model.endswith(":free") for model in captured["models"])
        )
        self.assertEqual(captured["reasoning"], {"effort": "low", "exclude": True})
        self.assertEqual(captured["response_format"], {"type": "json_object"})
        self.assertEqual(captured["max_tokens"], 2400)


class BoundedWriterTests(unittest.TestCase):
    def _briefs(self, count: int) -> list[dict]:
        return [
            {
                "id": f"s{i + 1}",
                "purpose": f"purpose {i + 1}",
                "visual_query": "calm room",
                "on_screen_text": "",
                "emotion": "steady",
                "expected_seconds": 60,
            }
            for i in range(count)
        ]

    def test_eight_section_film_writes_three_bounded_batches(self) -> None:
        calls: list[tuple[str, list[str]]] = []

        def fake_call(api_key, prompt, model, *, expected_ids):
            del api_key, model
            calls.append((prompt, list(expected_ids)))
            return {
                section_id: {"narration": f"narration {section_id}", "key_point": f"key {section_id}"}
                for section_id in expected_ids
            }

        with patch.object(staged, "_call_with_schema_repair", side_effect=fake_call):
            result = batching._write_full_script_batched(
                "key",
                topic="موضوع",
                fmt="film",
                model="model",
                briefs=self._briefs(8),
                narrative_format="direct_cinematic",
                target_per_section=120,
                transition_variants=["t1", "t2", "t3", "", "", "", ""],
                editorial_intent_json='{"editorial_thesis":"x"}',
                research_json="{}",
                avoid_json="{}",
                policy_json="{}",
                revision_note="",
            )

        self.assertEqual(
            [ids for _, ids in calls],
            [["s1", "s2", "s3"], ["s4", "s5", "s6"], ["s7", "s8"]],
        )
        self.assertEqual(list(result), [f"s{i}" for i in range(1, 9)])
        self.assertIn("last_global_section=false", calls[0][0])
        self.assertIn("do not summarize", calls[0][0])
        self.assertIn("last_global_section=false", calls[1][0])
        self.assertIn('"id":"s3","key_point":"key s3"', calls[1][0])
        self.assertIn("last_global_section=true", calls[2][0])
        self.assertIn('"id":"s6","key_point":"key s6"', calls[2][0])

    def test_story_five_sections_uses_three_plus_two_not_per_section_fanout(self) -> None:
        calls: list[list[str]] = []

        def fake_call(api_key, prompt, model, *, expected_ids):
            del api_key, prompt, model
            calls.append(list(expected_ids))
            return {
                section_id: {"narration": f"narration {section_id}", "key_point": f"key {section_id}"}
                for section_id in expected_ids
            }

        with patch.object(staged, "_call_with_schema_repair", side_effect=fake_call):
            batching._write_full_script_batched(
                "key",
                topic="موضوع",
                fmt="story",
                model="model",
                briefs=self._briefs(5),
                narrative_format="story_analysis",
                target_per_section=84,
                transition_variants=["t1", "t2", "t3", ""],
                editorial_intent_json="{}",
                research_json="{}",
                avoid_json="{}",
                policy_json="{}",
                revision_note="",
            )

        self.assertEqual(calls, [["s1", "s2", "s3"], ["s4", "s5"]])


class BoundedScriptDoctorTests(unittest.TestCase):
    def test_eight_section_doctor_repairs_three_batches_and_preserves_order(self) -> None:
        sections = [
            SimpleNamespace(id=f"s{i + 1}", narration=("كلمة " * 120).strip(), key_point=f"key {i + 1}")
            for i in range(8)
        ]
        calls: list[list[str]] = []

        def fake_call(api_key, prompt, model, *, expected_ids):
            del api_key, model
            calls.append(list(expected_ids))
            if expected_ids == ["s1", "s2", "s3"]:
                self.assertIn("global sections\n1-3", prompt)
            return {
                section_id: {"narration": f"fixed {section_id}", "key_point": f"fixed key {section_id}"}
                for section_id in expected_ids
            }

        with patch.object(staged, "_call_with_schema_repair", side_effect=fake_call):
            result = batching._script_doctor_batched(
                "key",
                topic="موضوع",
                model="model",
                sections=sections,
                policy_json="{}",
                research_json="{}",
                editorial_intent_json="{}",
                narrative_format="direct_cinematic",
                issue_notes="- duplicate key point",
            )

        self.assertEqual(
            calls,
            [["s1", "s2", "s3"], ["s4", "s5", "s6"], ["s7", "s8"]],
        )
        self.assertEqual(list(result), [f"s{i}" for i in range(1, 9)])


if __name__ == "__main__":
    unittest.main()
