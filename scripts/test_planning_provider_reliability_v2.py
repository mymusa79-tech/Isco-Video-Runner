from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.task_level_planner_router as router
from scripts.provider_failure import classify_provider_failure
from isco_video_agent.anti_repetition import novelty_context
from isco_video_agent.config import load_approved_brief, load_editorial_policy
from isco_video_agent.learning import learning_context
import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.resilient_planner as staged
from isco_video_agent.providers.gemini import with_channel_persona as engine_persona


class _Response:
    def __init__(self, status_code: int, payload: dict, headers: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


def _chat_ok(
    data: dict,
    *,
    finish_reason: str = "stop",
    model: str | None = None,
    usage: dict | None = None,
) -> _Response:
    payload = {
        "choices": [{"finish_reason": finish_reason, "message": {"content": json.dumps(data)}}]
    }
    if model is not None:
        payload["model"] = model
    if usage is not None:
        payload["usage"] = usage
    return _Response(200, payload)


def _outline_prompt(n: int = 2) -> str:
    return (
        f"Required number of sections: exactly {n}\n"
        "Return ONLY JSON with section_briefs and title_options.\n"
        "section_briefs must be exact."
    )


class PlanningProviderReliabilityV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for name in ("gemini", "groq", "openrouter"):
            (root / name).write_text("fake-key", encoding="utf-8")
        self.env = patch.dict(os.environ, {
            "GEMINI_API_KEY_FILE": str(root / "gemini"),
            "GROQ_API_KEY_FILE": str(root / "groq"),
            "OPENROUTER_API_KEY_FILE": str(root / "openrouter"),
        }, clear=False)
        self.cache = patch.object(router, "CACHE_PATH", root / "checkpoint.json")
        self.interval = patch.object(router, "MIN_PROVIDER_CALL_INTERVAL_SECONDS", 0.0)
        self.persona = patch.object(router, "with_channel_persona", side_effect=lambda prompt: prompt)
        self.sleep = patch.object(router.time, "sleep")
        self.env.start(); self.cache.start(); self.interval.start(); self.persona.start(); self.sleep.start()
        self.orig_json_text = staged.json_text
        self.orig_build = staged.build_plan
        self.orig_orchestrator_build = orchestrator.build_plan
        router._TELEMETRY.clear(); router._USED_PROVIDERS.clear(); router._last_call_rate_limit_headers.clear()
        router._last_call_response_meta.clear()

    def tearDown(self) -> None:
        staged.json_text = self.orig_json_text
        staged.build_plan = self.orig_build
        orchestrator.build_plan = self.orig_orchestrator_build
        self.sleep.stop(); self.persona.stop(); self.interval.stop(); self.cache.stop(); self.env.stop(); self.tmp.cleanup()

    def test_run115_disconnect_is_transient_network_failure(self) -> None:
        failure = classify_provider_failure("gemini", RuntimeError("Server disconnected without sending a response."))
        self.assertEqual(failure.telemetry_result, "network_error")
        self.assertFalse(failure.open_circuit)

    def test_disconnect_gets_exactly_one_bounded_retry_then_succeeds(self) -> None:
        calls = {"n": 0}
        def gemini(*args, **kwargs):
            del args, kwargs
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("Server disconnected without sending a response.")
            return {"ok": True}
        with patch.object(router, "gemini_json_text", side_effect=gemini):
            router.install_router()
            result = staged.json_text("unused", "small prompt", model="gemini-2.5-flash")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls["n"], 2)
        self.assertEqual([x["result"] for x in router.get_telemetry()], ["network_error", "success"])

    def test_quota_exhaustion_does_not_waste_transient_retry(self) -> None:
        gemini_calls = {"n": 0}
        def gemini(*args, **kwargs):
            del args, kwargs
            gemini_calls["n"] += 1
            raise RuntimeError("429 RESOURCE_EXHAUSTED: exceeded current quota")
        with patch.object(router, "gemini_json_text", side_effect=gemini), patch.object(router, "_groq_call", return_value={"ok": True}):
            router.install_router()
            result = staged.json_text("unused", "small prompt", model="gemini-2.5-flash")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(gemini_calls["n"], 1)

    def test_retry_after_is_respected_but_clamped(self) -> None:
        base = router._retry_delay_seconds("groq", 0)
        self.assertGreaterEqual(router._retry_delay_seconds("groq", 0, "12"), 12.0)
        self.assertEqual(router._retry_after_seconds("999"), router.RETRY_AFTER_MAX_SECONDS)
        self.assertGreaterEqual(base, router.TRANSIENT_RETRY_BASE_SECONDS)

    def test_groq_large_prompt_is_rejected_before_http_call(self) -> None:
        with patch.object(router, "GROQ_MAX_PROMPT_UTF8_BYTES", 16), patch.object(router.requests, "post") as post:
            with self.assertRaisesRegex(RuntimeError, "PAYLOAD_TOO_LARGE_PREFLIGHT"):
                router._groq_call("x" * 64)
        post.assert_not_called()

    def test_groq_request_size_failure_does_not_poison_later_small_request(self) -> None:
        with patch.object(router, "GROQ_MAX_PROMPT_UTF8_BYTES", 16), patch.object(router.requests, "post", return_value=_chat_ok({"ok": True})) as post:
            with self.assertRaises(RuntimeError):
                router._groq_call("x" * 64)
            with patch.object(router, "GROQ_MAX_PROMPT_UTF8_BYTES", 1024):
                result = router._groq_call("small")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(post.call_count, 1)

    def test_run116_exact_envelope_survives_gemini_500_and_reaches_groq(self) -> None:
        brief = load_approved_brief(required=True)
        prompt = staged.build_outline_prompt(
            topic=brief["approved_topic"],
            fmt="film",
            policy_json=json.dumps(load_editorial_policy(), ensure_ascii=False),
            research_json=json.dumps(
                orchestrator.planning_research_context(brief, {}), ensure_ascii=False
            ),
            avoid_json=json.dumps(novelty_context(), ensure_ascii=False),
            learning_json=json.dumps(learning_context("film"), ensure_ascii=False),
            revision_note="",
        )
        groq_prompts: list[str] = []

        def healthy_groq(value: str) -> dict:
            groq_prompts.append(value)
            return {"ok": True}

        with patch.object(router, "with_channel_persona", side_effect=engine_persona), \
                patch.object(router, "gemini_json_text", side_effect=RuntimeError("HTTP 500 high demand")) as gemini, \
                patch.object(router, "_groq_call", side_effect=healthy_groq), \
                patch.object(router, "_openrouter_call_with_repair") as openrouter:
            router.install_router()
            result = staged.json_text("unused", prompt, model="gemini-2.5-flash")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(gemini.call_count, 2)
        self.assertEqual(len(groq_prompts), 1)
        self.assertLessEqual(
            len(groq_prompts[0].encode("utf-8")),
            router.GROQ_MAX_PROMPT_UTF8_BYTES,
        )
        openrouter.assert_not_called()
        self.assertEqual(
            [entry["result"] for entry in router.get_telemetry()],
            ["server_error", "server_error", "success"],
        )

    def test_groq_known_contract_uses_strict_json_schema(self) -> None:
        seen: dict = {}
        def post(url, **kwargs):
            seen["url"] = url; seen["json"] = kwargs["json"]
            return _chat_ok({"section_briefs": []})
        with patch.object(router, "GROQ_MAX_PROMPT_UTF8_BYTES", 100000), patch.object(router.requests, "post", side_effect=post):
            router._groq_call(_outline_prompt(2))
        fmt = seen["json"]["response_format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertTrue(fmt["json_schema"]["strict"])
        self.assertEqual(fmt["json_schema"]["name"], "editorial_outline")
        self.assertFalse(fmt["json_schema"]["schema"]["additionalProperties"])
        self.assertEqual(seen["json"]["reasoning_effort"], "low")

    def test_groq_length_finish_reason_is_explicit_truncation(self) -> None:
        with patch.object(router.requests, "post", return_value=_chat_ok({"ok": True}, finish_reason="length")):
            with self.assertRaisesRegex(RuntimeError, "PREMATURE_RESPONSE"):
                router._groq_call("small")

    def test_openrouter_structured_request_enables_healing_and_free_failover(self) -> None:
        seen: dict = {}
        def post(url, **kwargs):
            seen["url"] = url; seen["json"] = kwargs["json"]
            return _chat_ok({"ok": True})
        contract = ("section_repair", router._strict_object({"narration": {"type": "string"}}, ["narration"]))
        with patch.object(router.requests, "post", side_effect=post):
            result = router._openrouter_structured_request("prompt", contract)
        self.assertEqual(result, {"ok": True})
        payload = seen["json"]
        self.assertEqual(payload["models"], ["openrouter/free", "openai/gpt-oss-20b:free"])
        self.assertEqual(payload["plugins"], [{"id": "response-healing"}])
        self.assertTrue(payload["provider"]["allow_fallbacks"])
        self.assertTrue(payload["provider"]["require_parameters"])
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertNotIn("reasoning", payload)

    def test_openrouter_outline_reserves_completion_budget_with_low_reasoning(self) -> None:
        seen: dict = {}
        def post(url, **kwargs):
            del url
            seen["json"] = kwargs["json"]
            return _chat_ok({"section_briefs": []})
        contract = router._structured_schema_for_prompt(_outline_prompt(2))
        with patch.object(router.requests, "post", side_effect=post):
            router._openrouter_structured_request(_outline_prompt(2), contract)
        self.assertEqual(
            seen["json"]["reasoning"], {"effort": "low", "exclude": True}
        )

    def test_openrouter_malformed_json_uses_compact_repair_not_full_prompt_replay(self) -> None:
        original = "ORIGINAL_SECRET_MARKER " + ("z" * 20000)
        malformed = _Response(200, {"choices": [{"finish_reason": "stop", "message": {"content": '{"narration": "ok"'}}]})
        repaired = _chat_ok({"narration": "ok"})
        payloads: list[dict] = []
        def post(url, **kwargs):
            del url
            payloads.append(kwargs["json"])
            return malformed if len(payloads) == 1 else repaired
        contract = ("section_repair", router._strict_object({"narration": {"type": "string"}}, ["narration"]))
        with patch.object(router.requests, "post", side_effect=post):
            result = router._openrouter_call_with_repair(original, "openrouter/free", response_contract=contract)
        self.assertEqual(result, {"narration": "ok"})
        self.assertEqual(len(payloads), 2)
        second_prompt = payloads[1]["messages"][0]["content"]
        self.assertNotIn("ORIGINAL_SECRET_MARKER", second_prompt)
        self.assertIn("MALFORMED_OUTPUT", second_prompt)
        self.assertLess(len(second_prompt), router._OPENROUTER_COMPACT_REPAIR_MAX_CHARS + 1000)

    def test_openrouter_truncation_is_never_sent_to_syntax_repair(self) -> None:
        contract = ("section_repair", router._strict_object({"narration": {"type": "string"}}, ["narration"]))
        with patch.object(router.requests, "post", return_value=_chat_ok({"narration": "partial"}, finish_reason="length")) as post:
            with self.assertRaisesRegex(RuntimeError, "PREMATURE_RESPONSE"):
                router._openrouter_call_with_repair("original", "openrouter/free", response_contract=contract)
        self.assertEqual(post.call_count, 1)

    def test_telemetry_contains_size_and_contract_but_never_prompt(self) -> None:
        marker = "DO_NOT_STORE_THIS_PROMPT"
        with patch.object(router, "gemini_json_text", return_value={"ok": True}):
            router.install_router(); staged.json_text("unused", marker, model="gemini-2.5-flash")
        entry = router.get_telemetry()[0]
        self.assertEqual(entry["response_contract"], "json_object")
        self.assertGreater(entry["prompt_utf8_bytes"], 0)
        self.assertNotIn(marker, json.dumps(entry, ensure_ascii=False))

    def test_provider_usage_metadata_is_recorded_without_response_content(self) -> None:
        response = _chat_ok(
            {"result": "RESPONSE_BODY_MARKER_MUST_NOT_BE_STORED"},
            model="openai/gpt-oss-20b",
            usage={
                "prompt_tokens": 1200,
                "completion_tokens": 700,
                "completion_tokens_details": {"reasoning_tokens": 180},
            },
        )
        with patch.object(router, "gemini_json_text", side_effect=RuntimeError("HTTP 403")), \
                patch.object(router.requests, "post", return_value=response):
            router.install_router()
            staged.json_text("unused", "small prompt", model="gemini-2.5-flash")
        entry = router.get_telemetry()[-1]
        self.assertEqual(entry["resolved_model"], "openai/gpt-oss-20b")
        self.assertEqual(entry["prompt_tokens"], 1200)
        self.assertEqual(entry["completion_tokens"], 700)
        self.assertEqual(entry["reasoning_tokens"], 180)
        self.assertNotIn(
            "RESPONSE_BODY_MARKER_MUST_NOT_BE_STORED",
            json.dumps(entry, ensure_ascii=False),
        )


if __name__ == "__main__":
    unittest.main()
