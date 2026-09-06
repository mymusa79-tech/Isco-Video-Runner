from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from scripts import planning_batch_hardening as batching
from scripts import planning_capacity_profile as capacity_profile
from scripts import planning_stage_contract as planning_contract
from scripts import provider_capacity_hardening as capacity
from scripts import task_level_planner_router as router
from scripts.provider_failure import classify_provider_failure


class _Response:
    def __init__(self, body: dict, *, status_code: int = 200):
        self._body = body
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.headers = {}

    def json(self):
        return self._body


FULL_SCRIPT_PROMPT = '''
Write a bounded batch.
Return ONLY JSON: {"sections": [{"id":"s1","narration":"...","key_point":"..."}]}
with EXACTLY 3 entries, in this exact order.
'''


def _writer_spec() -> planning_contract.PlanningStageSpec:
    return planning_contract.script_stage_spec("full_script", ["s1", "s2", "s3"])


class Run118BatchHeadroomTests(unittest.TestCase):
    def test_film_transport_maximum_is_three_three_two(self) -> None:
        self.assertEqual(batching.MAX_SCRIPT_BATCH_SECTIONS, 3)
        chunks = [part for _start, part in batching._chunks(list(range(8)))]
        self.assertEqual([len(part) for part in chunks], [3, 3, 2])

    def test_story_transport_maximum_is_three_two(self) -> None:
        chunks = [part for _start, part in batching._chunks(list(range(5)))]
        self.assertEqual([len(part) for part in chunks], [3, 2])


class Run118GroqGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        router._last_call_rate_limit_headers.clear()
        router._last_call_response_meta.clear()
        capacity_profile.install_explicit_planning_transport_projection()
        # Production's explicit Planning router owns this compatibility seam. Keep the
        # focused Run118 test on that same seam instead of accidentally reviving the
        # retired prompt-inference helper merely because this unit test calls the lower
        # Groq transport directly.
        self._old_schema_resolver = router._structured_schema_for_prompt
        router._structured_schema_for_prompt = planning_contract._explicit_schema_adapter

    def tearDown(self) -> None:
        router._structured_schema_for_prompt = self._old_schema_resolver

    def test_output_heavy_groq_uses_json_object_and_low_reasoning(self) -> None:
        captured = {}
        content = json.dumps({
            "sections": [
                {"id": "s1", "narration": "أ", "key_point": "١"},
                {"id": "s2", "narration": "ب", "key_point": "٢"},
                {"id": "s3", "narration": "ج", "key_point": "٣"},
            ]
        }, ensure_ascii=False)

        def fake_post(url, *, headers, json, timeout):
            captured.update(json)
            return _Response({
                "model": "openai/gpt-oss-20b",
                "choices": [{"finish_reason": "stop", "message": {"content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 200},
            })

        # Use the same bounded Writer scope installed around production shards. This
        # proves schema identity and the 1800-token reserve come from Stage Contract,
        # not from matching the words in FULL_SCRIPT_PROMPT.
        with planning_contract.script_batch_scope("writer", ["s1", "s2", "s3"]), \
                patch.object(router, "_read_secret_file", return_value="test-key"), \
                patch.object(router.requests, "post", side_effect=fake_post):
            result = capacity._hardened_groq_call(FULL_SCRIPT_PROMPT)

        self.assertEqual(len(result["sections"]), 3)
        self.assertEqual(captured["response_format"], {"type": "json_object"})
        self.assertEqual(captured["reasoning_effort"], "low")
        self.assertFalse(captured["include_reasoning"])
        self.assertEqual(captured["max_completion_tokens"], 1800)

    def test_outline_keeps_strict_schema(self) -> None:
        response_contract = planning_contract._schema_tuple(
            planning_contract.outline_stage_spec(8)
        )
        self.assertEqual(response_contract[0], "editorial_outline")
        response_format = capacity._response_format_for_contract(response_contract)
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])

    def test_json_validate_failed_is_request_scoped_generation_error(self) -> None:
        failure = classify_provider_failure(
            "groq",
            RuntimeError(
                "GROQ_JSON_VALIDATE_FAILED status=400 code=json_validate_failed"
            ),
        )
        self.assertEqual(failure.telemetry_result, "generation_error")
        self.assertFalse(failure.open_circuit)


class Run118OpenRouterFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        router._last_call_rate_limit_headers.clear()
        router._last_call_response_meta.clear()
        capacity_profile.install_explicit_planning_transport_projection()

    def test_output_heavy_openrouter_keeps_free_only_failover_and_minimal_reasoning(self) -> None:
        captured = {}
        response_contract = planning_contract._schema_tuple(_writer_spec())
        self.assertEqual(response_contract[0], "script_writer_3")
        content = json.dumps({
            "sections": [
                {"id": "s1", "narration": "أ", "key_point": "١"},
                {"id": "s2", "narration": "ب", "key_point": "٢"},
                {"id": "s3", "narration": "ج", "key_point": "٣"},
            ]
        }, ensure_ascii=False)

        def fake_post(url, *, headers, json, timeout):
            captured.update(json)
            return _Response({
                "model": capacity.OPENROUTER_OUTPUT_HEAVY_MODEL,
                "choices": [{"finish_reason": "stop", "message": {"content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 200},
            })

        with patch.object(router, "_openrouter_key", return_value="test-key"), patch.object(
            router.requests, "post", side_effect=fake_post
        ):
            result = capacity._hardened_openrouter_structured_request(
                FULL_SCRIPT_PROMPT, response_contract
            )

        self.assertEqual(len(result["sections"]), 3)
        self.assertEqual(captured["models"], list(capacity.OPENROUTER_OUTPUT_HEAVY_MODELS))
        self.assertEqual(captured["models"][-1], "openrouter/free")
        self.assertTrue(all(model.endswith(":free") for model in captured["models"][:-1]))
        self.assertEqual(captured["response_format"], {"type": "json_object"})
        self.assertEqual(captured["reasoning"], {"effort": "minimal", "exclude": True})
        self.assertEqual(captured["max_tokens"], 1800)
        self.assertTrue(captured["provider"]["allow_fallbacks"])
        self.assertTrue(captured["provider"]["require_parameters"])


if __name__ == "__main__":
    unittest.main()
