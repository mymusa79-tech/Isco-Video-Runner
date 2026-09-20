from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from clean_v2 import mistral_executor
from clean_v2 import providers
from clean_v2 import text_audit
from clean_v2 import visual_qa
from clean_v2.pipeline import CleanV2Pipeline, _build_production_plan_for_audit


_FACT_PASS = {
    "status": "pass",
    "unsupported_claims": [],
    "professional_advice_flags": [],
    "expert_persona_flags": [],
    "notes": [],
}


class _Response:
    status = 200
    headers = {
        "x-ratelimit-limit-req-minute": "30",
        "x-ratelimit-limit-tokens-minute": "937500",
        "x-ratelimit-remaining-tokens-minute": "936900",
    }

    def __init__(self, result: dict) -> None:
        self.result = result

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return self.status

    def read(self, _limit: int):
        body = {
            "model": mistral_executor.MISTRAL_EXECUTOR_MODEL,
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(self.result)},
                }
            ],
            "usage": {
                "prompt_tokens": 400,
                "completion_tokens": 200,
                "total_tokens": 600,
            },
        }
        return json.dumps(body).encode("utf-8")


def _brief() -> dict:
    return {
        "approved_topic": "اختبار واقعي كامل",
        "pillar": "personal_development",
        "format": "film",
        "research_pack": [],
    }


def _plan() -> dict:
    return {
        "title": "عنوان الاختبار",
        "promise": "وعد واضح للمشاهد",
        "sections": [
            {
                "id": f"s{index}",
                "heading": f"القسم {index}",
                "purpose": "غرض كامل وواضح لهذا القسم",
                "visual_query_en": f"calm daily routine detail {index}",
            }
            for index in range(1, 6)
        ],
    }


def _script() -> dict:
    return {
        "title": "عنوان الاختبار",
        "sections": [
            {
                "id": f"s{index}",
                "narration": (
                    "هذا نص عربي كامل بما يكفي لاختبار عقد التدقيق الفعلي "
                    "من دون ادعاءات خارج سياق البحث المعتمد."
                ),
            }
            for index in range(1, 6)
        ],
    }


class MistralExecutorTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        mistral_executor.reset_mistral_executor_telemetry()

    def test_transport_records_real_usage_headers_and_executor_role(self) -> None:
        self.assertEqual(
            mistral_executor.MISTRAL_PLANNING_SCRIPT_MODEL,
            "ministral-14b-2512",
        )
        with mock.patch.dict(
            os.environ,
            {
                "MISTRAL_API_KEY": "test-key",
                "MISTRAL_CONTENT_MODEL": "ministral-14b-2512",
            },
            clear=False,
        ), mock.patch.object(
            mistral_executor.urllib.request,
            "urlopen",
            return_value=_Response({"ok": True}),
        ) as urlopen:
            result = mistral_executor.mistral_executor_json(
                "full production prompt",
                max_tokens=7500,
                task_kind="script",
            )

        self.assertEqual(result, {"ok": True})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, mistral_executor.MISTRAL_CHAT_URL)
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "ministral-14b-2512")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["max_tokens"], 7500)
        telemetry = mistral_executor.get_mistral_executor_telemetry()
        self.assertEqual(len(telemetry), 1)
        self.assertEqual(telemetry[0]["role"], "executor")
        self.assertEqual(telemetry[0]["task_kind"], "script")
        self.assertEqual(telemetry[0]["model"], "ministral-14b-2512")
        self.assertEqual(telemetry[0]["usage"]["total_tokens"], 600)
        self.assertEqual(
            telemetry[0]["rate_limit_headers"][
                "x-ratelimit-limit-tokens-minute"
            ],
            "937500",
        )

    def test_visual_query_recovery_uses_verified_content_model_and_strict_schema(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "MISTRAL_API_KEY": "test-key",
                "MISTRAL_CONTENT_MODEL": "ministral-14b-2512",
            },
            clear=False,
        ), mock.patch.object(
            mistral_executor.urllib.request,
            "urlopen",
            return_value=_Response({"alternate_query": "focused alternate query"}),
        ) as urlopen:
            result = mistral_executor.mistral_executor_json(
                "generate one alternate visual query for section s5",
                max_tokens=300,
                task_kind="visual_query_recovery",
                response_schema=providers.MISTRAL_VISUAL_QUERY_RECOVERY_SCHEMA,
            )

        self.assertEqual(result, {"alternate_query": "focused alternate query"})
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "ministral-14b-2512")
        self.assertEqual(payload["max_tokens"], 300)
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertEqual(
            payload["response_format"]["json_schema"]["schema"],
            providers.MISTRAL_VISUAL_QUERY_RECOVERY_SCHEMA[1],
        )
        telemetry = mistral_executor.get_mistral_executor_telemetry()
        self.assertEqual(telemetry[-1]["task_kind"], "visual_query_recovery")
        self.assertEqual(telemetry[-1]["model"], "ministral-14b-2512")

    def test_narrative_identity_keeps_executor_model_boundary(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "MISTRAL_API_KEY": "test-key",
                "MISTRAL_CONTENT_MODEL": "mistral-small-2603",
            },
            clear=False,
        ), mock.patch.object(
            mistral_executor.urllib.request,
            "urlopen",
            return_value=_Response({"ok": True}),
        ) as urlopen:
            result = mistral_executor.mistral_executor_json(
                "identity production prompt",
                max_tokens=900,
                task_kind="narrative_identity",
            )

        self.assertEqual(result, {"ok": True})
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "ministral-14b-2512")
        self.assertEqual(payload["max_tokens"], 900)
        telemetry = mistral_executor.get_mistral_executor_telemetry()
        self.assertEqual(telemetry[-1]["task_kind"], "narrative_identity")
        self.assertEqual(telemetry[-1]["model"], "ministral-14b-2512")

    def test_text_audit_keeps_existing_ministral_model_boundary(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "MISTRAL_API_KEY": "test-key",
                "MISTRAL_CONTENT_MODEL": "mistral-small-2603",
            },
            clear=False,
        ), mock.patch.object(
            mistral_executor.urllib.request,
            "urlopen",
            return_value=_Response(dict(_FACT_PASS)),
        ) as urlopen:
            result = mistral_executor.mistral_executor_json(
                "audit production script",
                max_tokens=2200,
                task_kind="text_audit",
            )

        self.assertEqual(result, _FACT_PASS)
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "ministral-14b-2512")
        telemetry = mistral_executor.get_mistral_executor_telemetry()
        self.assertEqual(telemetry[-1]["task_kind"], "text_audit")
        self.assertEqual(telemetry[-1]["model"], "ministral-14b-2512")

    def test_gold_or_unknown_role_is_rejected_before_wire(self) -> None:
        with mock.patch.object(
            mistral_executor.urllib.request, "urlopen"
        ) as urlopen:
            with self.assertRaises(
                mistral_executor.MistralExecutorNoWireFailure
            ) as raised:
                mistral_executor.mistral_executor_json(
                    "judge this content",
                    max_tokens=500,
                    task_kind="gold",
                )
        self.assertEqual(raised.exception.reason_code, "mistral_executor_role_not_allowed")
        urlopen.assert_not_called()


class CleanV2ProviderRoutingTests(unittest.TestCase):
    @staticmethod
    def _technical(name: str, order: list[str]):
        def fail(_prompt: str, _tokens: int):
            order.append(name)
            raise providers.ProviderWireFailure(f"{name}_technical")

        return fail

    def test_planning_and_script_use_exact_four_provider_order(self) -> None:
        for stage in ("planning", "script"):
            with self.subTest(stage=stage):
                order: list[str] = []

                def mistral_call(_prompt, *, max_tokens, task_kind, **_kwargs):
                    order.append("mistral")
                    self.assertEqual(task_kind, stage)
                    return {"ok": True}

                with mock.patch.object(
                    providers, "_gemini_call", side_effect=self._technical("gemini", order)
                ), mock.patch.object(
                    providers, "_groq_call", side_effect=self._technical("groq", order)
                ), mock.patch.object(
                    providers,
                    "_openrouter_call",
                    side_effect=self._technical("openrouter", order),
                ), mock.patch.object(
                    mistral_executor,
                    "mistral_executor_json",
                    side_effect=mistral_call,
                ):
                    router = providers.ProviderRouter()
                    result = router.route(
                        stage=stage,
                        prompt="full prompt",
                        max_tokens=7500,
                        validator=lambda value: value,
                    )

                self.assertEqual(result, {"ok": True})
                self.assertEqual(order, ["gemini", "groq", "openrouter", "mistral"])
                self.assertEqual(
                    [item["provider"] for item in router.events],
                    ["gemini", "groq", "openrouter", "mistral"],
                )
                self.assertEqual(router.events[-1]["stage_wire_attempt"], 4)

    def test_s5_visual_query_recovery_passes_strict_schema_to_mistral_fourth(self) -> None:
        order: list[str] = []
        original_query = "person looking at wall clock beside unfinished task"
        alternate_query = "person closing distracting phone and returning to desk task"

        def fail(reason: str, name: str):
            def _failure(_prompt: str, _tokens: int):
                order.append(name)
                raise providers.ProviderWireFailure(reason)
            return _failure

        def mistral_call(_prompt, *, max_tokens, task_kind, response_schema=None, **_kwargs):
            order.append("mistral")
            self.assertIn("s5", _prompt)
            self.assertEqual(task_kind, "visual_query_recovery")
            self.assertEqual(max_tokens, 300)
            self.assertEqual(
                response_schema,
                providers.MISTRAL_VISUAL_QUERY_RECOVERY_SCHEMA,
            )
            return {"alternate_query": alternate_query}

        with mock.patch.object(
            providers, "_gemini_call", side_effect=fail("http_503", "gemini")
        ), mock.patch.object(
            providers, "_groq_call", side_effect=fail("http_400", "groq")
        ), mock.patch.object(
            providers, "_openrouter_call", side_effect=fail("http_429", "openrouter")
        ), mock.patch.object(
            mistral_executor, "mistral_executor_json", side_effect=mistral_call
        ):
            router = providers.ProviderRouter()
            result = router.route(
                stage="visual_query_recovery",
                prompt="section=s5 recovery prompt",
                max_tokens=300,
                validator=lambda value: visual_qa._validate_alternate_query(
                    value,
                    original_query=original_query,
                ),
            )

        self.assertEqual(result, {"alternate_query": alternate_query})
        self.assertEqual(order, ["gemini", "groq", "openrouter", "mistral"])
        self.assertEqual(
            [item["provider"] for item in router.events],
            ["gemini", "groq", "openrouter", "mistral"],
        )
        self.assertEqual(
            [item["reason"] for item in router.events[:3]],
            ["http_503", "http_400", "http_429"],
        )
        self.assertEqual(router.events[-1]["result"], "success")
        self.assertEqual(router.events[-1]["stage_wire_attempt"], 4)

    def test_s5_mistral_validator_failure_logs_raw_content(self) -> None:
        invalid = {"alternate_query": "same query"}
        raw = json.dumps(invalid)

        def mistral_call(_prompt, *, max_tokens, task_kind, response_schema=None, **_kwargs):
            del _prompt, max_tokens, task_kind, response_schema
            return invalid

        with mock.patch.object(
            mistral_executor, "mistral_executor_json", side_effect=mistral_call
        ), mock.patch.object(
            mistral_executor,
            "get_last_mistral_executor_raw_content",
            return_value=raw,
        ), mock.patch("builtins.print") as emit:
            router = providers.ProviderRouter(
                (
                    providers.ProviderAdapter(
                        "mistral",
                        providers._mistral_call,
                        stages=frozenset({"visual_query_recovery"}),
                        accepts_stage=True,
                    ),
                )
            )
            with self.assertRaises(RuntimeError):
                router.route(
                    stage="visual_query_recovery",
                    prompt="section=s5 recovery prompt",
                    max_tokens=300,
                    validator=lambda value: visual_qa._validate_alternate_query(
                        value,
                        original_query="same query",
                    ),
                )

        emitted = "\n".join(str(call.args[0]) for call in emit.call_args_list)
        self.assertIn("validator rejected raw content", emitted)
        self.assertIn("alternate_query", emitted)
        self.assertIn("same query", emitted)

    def test_http_429_captures_numeric_retry_after_header(self) -> None:
        error = providers.urllib.error.HTTPError(
            "https://provider.invalid/v1",
            429,
            "rate limited",
            {"Retry-After": "7"},
            None,
        )
        with mock.patch.object(
            providers.urllib.request,
            "urlopen",
            side_effect=error,
        ):
            with self.assertRaises(providers.ProviderWireFailure) as raised:
                providers._post_json(
                    "https://provider.invalid/v1",
                    headers={},
                    payload={"test": True},
                    timeout=1,
                )

        self.assertEqual(raised.exception.reason_code, "http_429")
        self.assertEqual(raised.exception.http_status, 429)
        self.assertEqual(raised.exception.retry_after_seconds, 7.0)

    def test_short_retry_after_waits_once_before_next_planning_provider(self) -> None:
        calls: list[str] = []

        def rate_limited(_prompt: str, _tokens: int) -> dict:
            calls.append("gemini")
            raise providers.ProviderWireFailure(
                "http_429",
                http_status=429,
                retry_after_seconds=7.0,
            )

        def succeeds(_prompt: str, _tokens: int) -> dict:
            calls.append("groq")
            return {"ok": True}

        router = providers.ProviderRouter(
            (
                providers.ProviderAdapter("gemini", rate_limited),
                providers.ProviderAdapter("groq", succeeds),
            )
        )
        with mock.patch.object(providers.time, "sleep") as sleep:
            result = router.route(
                stage="planning",
                prompt="full prompt",
                max_tokens=7500,
                validator=lambda value: value,
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, ["gemini", "groq"])
        sleep.assert_called_once_with(7.0)

    def test_long_or_missing_retry_after_moves_immediately_in_one_pass(self) -> None:
        for retry_after in (None, 10.1):
            with self.subTest(retry_after=retry_after):
                calls: list[str] = []

                def rate_limited(_prompt: str, _tokens: int) -> dict:
                    calls.append("gemini")
                    raise providers.ProviderWireFailure(
                        "http_429",
                        http_status=429,
                        retry_after_seconds=retry_after,
                    )

                def succeeds(_prompt: str, _tokens: int) -> dict:
                    calls.append("groq")
                    return {"ok": True}

                router = providers.ProviderRouter(
                    (
                        providers.ProviderAdapter("gemini", rate_limited),
                        providers.ProviderAdapter("groq", succeeds),
                    )
                )
                with mock.patch.object(providers.time, "sleep") as sleep:
                    result = router.route(
                        stage="script",
                        prompt="full prompt",
                        max_tokens=7500,
                        validator=lambda value: value,
                    )

                self.assertEqual(result, {"ok": True})
                self.assertEqual(calls, ["gemini", "groq"])
                sleep.assert_not_called()

    def test_narrative_identity_reaches_mistral_fourth_without_retry_sleep(self) -> None:
        order: list[str] = []

        def mistral_call(_prompt, *, max_tokens, task_kind, **_kwargs):
            order.append("mistral")
            self.assertEqual(task_kind, "narrative_identity")
            self.assertEqual(max_tokens, 1000)
            return {"ok": True}

        with mock.patch.object(
            providers, "_gemini_call", side_effect=self._technical("gemini", order)
        ), mock.patch.object(
            providers, "_groq_call", side_effect=self._technical("groq", order)
        ), mock.patch.object(
            providers,
            "_openrouter_call",
            side_effect=self._technical("openrouter", order),
        ), mock.patch.object(
            mistral_executor,
            "mistral_executor_json",
            side_effect=mistral_call,
        ), mock.patch.object(providers.time, "sleep") as sleep:
            router = providers.ProviderRouter()
            result = router.route(
                stage="narrative_identity",
                prompt="identity prompt",
                max_tokens=1000,
                validator=lambda value: value,
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(order, ["gemini", "groq", "openrouter", "mistral"])
        self.assertEqual(
            [item["provider"] for item in router.events],
            ["gemini", "groq", "openrouter", "mistral"],
        )
        self.assertEqual(router.events[-1]["stage_wire_attempt"], 4)
        sleep.assert_not_called()


class CleanV2TextAuditRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.production_plan = _build_production_plan_for_audit(
            brief=_brief(),
            plan=_plan(),
            script=_script(),
        )

    @staticmethod
    def _failure(name: str, calls: list[str]):
        def fail(*_args, **_kwargs):
            calls.append(name)
            raise RuntimeError(f"{name} technical capacity failure")

        return fail

    def test_three_technical_failures_reach_mistral_last(self) -> None:
        from isco_video_agent import factuality

        calls: list[str] = []
        original_route = factuality.route_text_audit

        def mistral_pass(_prompt: str):
            calls.append("mistral")
            return dict(_FACT_PASS)

        diagnostics: dict = {}
        with mock.patch.object(
            factuality, "json_text", side_effect=self._failure("gemini", calls)
        ), mock.patch.object(
            factuality.groq,
            "json_text",
            side_effect=self._failure("groq", calls),
        ), mock.patch.object(
            factuality.openrouter,
            "json_text",
            side_effect=self._failure("openrouter", calls),
        ), mock.patch.object(
            text_audit,
            "_mistral_factuality_call",
            side_effect=mistral_pass,
        ):
            result = text_audit.audit_plan_with_mistral(
                "gemini-key",
                self.production_plan,
                [],
                "gemini-3.7-flash",
                diagnostics=diagnostics,
            )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(calls, ["gemini", "groq", "openrouter", "mistral"])
        self.assertEqual(diagnostics["provider"], "mistral")
        self.assertEqual(
            [item["provider"] for item in diagnostics["attempts"]],
            ["gemini", "groq", "openrouter", "mistral"],
        )
        self.assertIs(factuality.route_text_audit, original_route)

    def test_semantic_block_is_final_and_does_not_call_mistral(self) -> None:
        from isco_video_agent import factuality

        blocked = dict(_FACT_PASS)
        blocked["status"] = "block"
        blocked["unsupported_claims"] = ["unsupported claim"]
        with mock.patch.object(
            factuality, "json_text", return_value=blocked
        ), mock.patch.object(
            factuality.groq, "json_text"
        ) as groq_call, mock.patch.object(
            factuality.openrouter, "json_text"
        ) as openrouter_call, mock.patch.object(
            text_audit, "_mistral_factuality_call"
        ) as mistral_call:
            result = text_audit.audit_plan_with_mistral(
                "gemini-key",
                self.production_plan,
                [],
                "gemini-3.7-flash",
            )

        self.assertEqual(result["status"], "block")
        groq_call.assert_not_called()
        openrouter_call.assert_not_called()
        mistral_call.assert_not_called()

    def test_mistral_schema_mismatch_is_final_fail_closed(self) -> None:
        from isco_video_agent import factuality

        calls: list[str] = []
        malformed = dict(_FACT_PASS)
        malformed.pop("notes")
        diagnostics: dict = {}
        with mock.patch.object(
            factuality, "json_text", side_effect=self._failure("gemini", calls)
        ), mock.patch.object(
            factuality.groq,
            "json_text",
            side_effect=self._failure("groq", calls),
        ), mock.patch.object(
            factuality.openrouter,
            "json_text",
            side_effect=self._failure("openrouter", calls),
        ), mock.patch.object(
            text_audit,
            "mistral_executor_json",
            return_value=malformed,
        ):
            result = text_audit.audit_plan_with_mistral(
                "gemini-key",
                self.production_plan,
                [],
                "gemini-3.7-flash",
                diagnostics=diagnostics,
            )

        self.assertEqual(result["status"], "block")
        self.assertEqual(
            result["unsupported_claims"],
            ["Factuality audit could not be completed safely"],
        )
        self.assertEqual(diagnostics["validation"], "providers_exhausted")
        self.assertEqual(diagnostics["attempts"][-1]["provider"], "mistral")
        self.assertEqual(diagnostics["attempts"][-1]["outcome"], "schema_invalid")


class MistralTelemetryPersistenceTests(unittest.TestCase):
    def test_pipeline_persists_executor_telemetry_only_when_called(self) -> None:
        pipeline = CleanV2Pipeline.__new__(CleanV2Pipeline)
        pipeline.router = SimpleNamespace(events=[])
        pipeline.visual_source = SimpleNamespace(events=[])
        entry = {
            "provider": "mistral",
            "role": "executor",
            "task_kind": "script",
            "usage": {"total_tokens": 1234},
            "rate_limit_headers": {
                "x-ratelimit-limit-tokens-minute": "937500"
            },
        }
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            mistral_executor,
            "get_mistral_executor_telemetry",
            return_value=[entry],
        ):
            output = Path(root)
            pipeline._write_runtime_events(output)
            saved = json.loads(
                (output / "mistral-executor-telemetry.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(saved["role"], "executor")
        self.assertEqual(saved["calls"], [entry])


if __name__ == "__main__":
    unittest.main()
