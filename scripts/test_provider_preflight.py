from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from scripts import provider_preflight as preflight


class ProviderPreflightTests(unittest.TestCase):
    def _response(self, status: int, payload: dict | None = None, headers: dict | None = None) -> Mock:
        response = Mock(spec=requests.Response)
        response.status_code = status
        response.ok = 200 <= status < 300
        response.json.return_value = payload if payload is not None else {}
        response.headers = headers or {}
        return response

    def _gemini_models(
        self,
        *,
        include_tts: bool = True,
        include_runtime: bool = True,
        include_requested: bool = False,
    ) -> Mock:
        models: list[dict] = []
        if include_runtime:
            models.append(
                {
                    "name": "models/gemini-3.7-flash",
                    "supportedGenerationMethods": ["generateContent", "countTokens"],
                }
            )
        if include_requested:
            models.append(
                {
                    "name": "models/gemini-2.5-flash",
                    "supportedGenerationMethods": ["generateContent", "countTokens"],
                }
            )
        if include_tts:
            models.append(
                {
                    "name": "models/gemini-3.1-flash-tts-preview",
                    "supportedGenerationMethods": ["generateContent"],
                }
            )
        return self._response(200, {"models": models})

    def _groq_headers(self, *, requests_left: str = "100", tokens_left: str = "2000") -> dict:
        return {
            "x-ratelimit-remaining-requests": requests_left,
            "x-ratelimit-remaining-tokens": tokens_left,
            "x-ratelimit-reset-requests": "2m59s",
        }

    def _pexels_headers(self, *, remaining: str = "100") -> dict:
        return {"X-Ratelimit-Remaining": remaining, "X-Ratelimit-Reset": "1893456000"}

    def _pixabay_headers(self, *, remaining: str = "90") -> dict:
        return {"X-RateLimit-Remaining": remaining, "X-RateLimit-Reset": "42"}

    def _gemini_pass(self) -> preflight.ProviderCheck:
        return preflight.ProviderCheck("gemini", "pass", 200, capacity_status="dynamic_unobservable")

    def _pexels_pass(self) -> preflight.ProviderCheck:
        return preflight.ProviderCheck("pexels", "pass", 200, capacity_status="positive", capacity_remaining=100)

    def test_gemini_certifies_exact_engine_resolved_model_without_counttokens(self) -> None:
        with patch.object(preflight.requests, "get", return_value=self._gemini_models()) as get, patch.object(
            preflight.requests, "post"
        ) as post:
            result = preflight.check_gemini(
                "secret",
                content_model="gemini-2.5-flash",
                tts_model="gemini-3.1-flash-tts-preview",
            )
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.capacity_status, "dynamic_unobservable")
        self.assertIn("gemini-3.7-flash", result.detail)
        self.assertEqual(get.call_count, 1)
        post.assert_not_called()

    def test_gemini_does_not_false_pass_requested_model_when_engine_resolves_elsewhere(self) -> None:
        with patch.object(
            preflight.requests,
            "get",
            return_value=self._gemini_models(include_runtime=False, include_requested=True),
        ):
            with self.assertRaisesRegex(RuntimeError, "resolved=gemini-3.7-flash"):
                preflight.check_gemini(
                    "secret",
                    content_model="gemini-2.5-flash",
                    tts_model="gemini-3.1-flash-tts-preview",
                )

    def test_gemini_requires_generatecontent_on_resolved_model(self) -> None:
        response = self._response(
            200,
            {
                "models": [
                    {
                        "name": "models/gemini-3.7-flash",
                        "supportedGenerationMethods": ["countTokens"],
                    }
                ]
            },
        )
        with patch.object(preflight.requests, "get", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "lacks generateContent"):
                preflight.check_gemini(
                    "secret",
                    content_model="gemini-2.5-flash",
                    tts_model="gemini-3.1-flash-tts-preview",
                )

    def test_gemini_missing_cloud_tts_is_observable_not_hard_block(self) -> None:
        with patch.object(preflight.requests, "get", return_value=self._gemini_models(include_tts=False)):
            result = preflight.check_gemini(
                "secret",
                content_model="gemini-2.5-flash",
                tts_model="gemini-3.1-flash-tts-preview",
            )
        self.assertEqual(result.status, "pass")
        self.assertIn("Piper fallback required", result.detail)

    def test_gemini_pagination_and_repeated_token_guard(self) -> None:
        first = self._response(
            200,
            {
                "models": [
                    {
                        "name": "models/gemini-3.7-flash",
                        "supportedGenerationMethods": ["generateContent"],
                    }
                ],
                "nextPageToken": "p2",
            },
        )
        second = self._response(
            200,
            {
                "models": [
                    {
                        "name": "models/gemini-3.1-flash-tts-preview",
                        "supportedGenerationMethods": ["generateContent"],
                    }
                ]
            },
        )
        with patch.object(preflight.requests, "get", side_effect=[first, second]):
            self.assertEqual(
                preflight.check_gemini(
                    "secret",
                    content_model="gemini-2.5-flash",
                    tts_model="gemini-3.1-flash-tts-preview",
                ).status,
                "pass",
            )

        repeated = self._response(200, {"models": [], "nextPageToken": "same"})
        with patch.object(preflight.requests, "get", side_effect=[repeated, repeated]):
            with self.assertRaisesRegex(RuntimeError, "repeated"):
                preflight.check_gemini(
                    "secret",
                    content_model="gemini-2.5-flash",
                    tts_model="gemini-3.1-flash-tts-preview",
                )

    def test_groq_models_endpoint_without_capacity_headers_is_not_false_block(self) -> None:
        response = self._response(200, {"data": [{"id": "openai/gpt-oss-20b"}]})
        with patch.object(preflight.requests, "get", return_value=response):
            result = preflight.check_groq("secret")
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.capacity_status, "dynamic_unobservable")
        self.assertIn("no capacity headers", result.detail)

    def test_groq_visible_capacity_is_enforced_when_present(self) -> None:
        response = self._response(
            200,
            {"data": [{"id": "openai/gpt-oss-20b"}]},
            self._groq_headers(),
        )
        with patch.object(preflight.requests, "get", return_value=response):
            result = preflight.check_groq("secret")
        self.assertEqual(result.capacity_status, "positive")
        self.assertEqual(result.capacity_remaining, 100)

        for headers in (
            {"x-ratelimit-remaining-requests": "0"},
            {"x-ratelimit-remaining-tokens": "0"},
        ):
            with self.subTest(headers=headers), patch.object(
                preflight.requests,
                "get",
                return_value=self._response(200, {"data": [{"id": "openai/gpt-oss-20b"}]}, headers),
            ):
                with self.assertRaisesRegex(RuntimeError, "no remaining capacity"):
                    preflight.check_groq("secret")

    def test_groq_requires_actual_runtime_fallback_model(self) -> None:
        with patch.object(
            preflight.requests,
            "get",
            return_value=self._response(200, {"data": [{"id": "other/model"}]}),
        ):
            with self.assertRaisesRegex(RuntimeError, "configured fallback model unavailable"):
                preflight.check_groq("secret")

    def test_openrouter_capacity_expiry_and_key_type_stay_fail_closed(self) -> None:
        healthy = self._response(
            200,
            {"data": {"limit": 100, "limit_remaining": 74.5, "limit_reset": "monthly"}},
        )
        with patch.object(preflight.requests, "get", return_value=healthy):
            result = preflight.check_openrouter("secret")
        self.assertEqual(result.capacity_status, "positive")
        self.assertEqual(result.capacity_remaining, 74.5)

        cases = [
            ({"data": {"limit": 10, "limit_remaining": 0}}, "exhausted"),
            ({"data": {"limit": None, "is_management_key": True}}, "administrative"),
            ({"data": {"limit": None, "expires_at": "2020-01-01T00:00:00Z"}}, "expired"),
        ]
        for payload, pattern in cases:
            with self.subTest(pattern=pattern), patch.object(
                preflight.requests, "get", return_value=self._response(200, payload)
            ):
                with self.assertRaisesRegex(RuntimeError, pattern):
                    preflight.check_openrouter("secret")

    def test_pexels_requires_positive_monthly_headroom(self) -> None:
        with patch.object(
            preflight.requests,
            "get",
            return_value=self._response(200, {"videos": []}, self._pexels_headers(remaining="123")),
        ):
            result = preflight.check_pexels("secret")
        self.assertEqual(result.capacity_remaining, 123)
        self.assertEqual(result.capacity_unit, "monthly requests")

        with patch.object(
            preflight.requests,
            "get",
            return_value=self._response(200, {"videos": []}, self._pexels_headers(remaining="0")),
        ):
            with self.assertRaisesRegex(RuntimeError, "no remaining monthly requests"):
                preflight.check_pexels("secret")

    def test_pixabay_requires_positive_window_headroom(self) -> None:
        with patch.object(
            preflight.requests,
            "get",
            return_value=self._response(200, {"hits": []}, self._pixabay_headers(remaining="80")),
        ):
            result = preflight.check_pixabay("secret")
        self.assertEqual(result.capacity_remaining, 80)

        with patch.object(
            preflight.requests,
            "get",
            return_value=self._response(200, {"hits": []}, self._pixabay_headers(remaining="0")),
        ):
            with self.assertRaisesRegex(RuntimeError, "no remaining requests"):
                preflight.check_pixabay("secret")

    def test_run111_openrouter_exhaustion_is_fallback_degradation_not_whole_run_veto(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            preflight, "check_gemini", return_value=self._gemini_pass()
        ), patch.object(
            preflight, "check_groq", return_value=preflight.ProviderCheck("groq", "pass", 200)
        ), patch.object(
            preflight,
            "check_openrouter",
            side_effect=RuntimeError("openrouter readiness blocked: key spend capacity exhausted"),
        ), patch.object(
            preflight, "check_pexels", return_value=self._pexels_pass()
        ), patch.object(
            preflight, "check_pixabay", return_value=preflight.ProviderCheck("pixabay", "pass", 200)
        ):
            output = Path(tmp) / "provider-preflight.json"
            results = preflight.run_preflight(
                gemini_key="a",
                groq_key="b",
                openrouter_key="c",
                pexels_key="d",
                pixabay_key="e",
                content_model="gemini-2.5-flash",
                tts_model="gemini-3.1-flash-tts-preview",
                output=output,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["overall_status"], "pass")
        self.assertIn("openrouter", payload["fallback_degraded"])
        self.assertEqual({item.provider: item.status for item in results}["openrouter"], "block")

    def test_optional_pixabay_failure_does_not_veto_healthy_hard_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            preflight, "check_gemini", return_value=self._gemini_pass()
        ), patch.object(
            preflight, "check_groq", return_value=preflight.ProviderCheck("groq", "pass", 200)
        ), patch.object(
            preflight, "check_openrouter", return_value=preflight.ProviderCheck("openrouter", "pass", 200)
        ), patch.object(
            preflight, "check_pexels", return_value=self._pexels_pass()
        ), patch.object(
            preflight, "check_pixabay", side_effect=RuntimeError("pixabay outage")
        ):
            output = Path(tmp) / "provider-preflight.json"
            preflight.run_preflight(
                gemini_key="a",
                groq_key="b",
                openrouter_key="c",
                pexels_key="d",
                pixabay_key="e",
                content_model="gemini-2.5-flash",
                tts_model="gemini-3.1-flash-tts-preview",
                output=output,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["overall_status"], "pass")
        self.assertIn("pixabay", payload["fallback_degraded"])

    def test_required_provider_failure_still_blocks_even_when_fallbacks_pass(self) -> None:
        for failed_provider in ("gemini", "pexels"):
            with self.subTest(failed_provider=failed_provider), tempfile.TemporaryDirectory() as tmp, patch.object(
                preflight,
                "check_gemini",
                side_effect=RuntimeError("gemini unavailable") if failed_provider == "gemini" else None,
                return_value=None if failed_provider == "gemini" else self._gemini_pass(),
            ), patch.object(
                preflight, "check_groq", return_value=preflight.ProviderCheck("groq", "pass", 200)
            ), patch.object(
                preflight, "check_openrouter", return_value=preflight.ProviderCheck("openrouter", "pass", 200)
            ), patch.object(
                preflight,
                "check_pexels",
                side_effect=RuntimeError("pexels unavailable") if failed_provider == "pexels" else None,
                return_value=None if failed_provider == "pexels" else self._pexels_pass(),
            ), patch.object(
                preflight, "check_pixabay", return_value=preflight.ProviderCheck("pixabay", "pass", 200)
            ):
                output = Path(tmp) / "provider-preflight.json"
                with self.assertRaisesRegex(RuntimeError, failed_provider):
                    preflight.run_preflight(
                        gemini_key="a",
                        groq_key="b",
                        openrouter_key="c",
                        pexels_key="d",
                        pixabay_key="e",
                        content_model="gemini-2.5-flash",
                        tts_model="gemini-3.1-flash-tts-preview",
                        output=output,
                    )
                payload = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(payload["overall_status"], "block")
                self.assertIn(failed_provider, payload["hard_failures"])

    def test_malformed_schema_and_http_failures_remain_provider_blocks(self) -> None:
        cases = [
            (preflight.check_groq, {"unexpected": []}, {}),
            (preflight.check_openrouter, {"data": []}, {}),
            (preflight.check_pexels, {"videos": {}}, self._pexels_headers()),
            (preflight.check_pixabay, {"hits": {}}, self._pixabay_headers()),
        ]
        for fn, payload, headers in cases:
            with self.subTest(fn=fn.__name__), patch.object(
                preflight.requests, "get", return_value=self._response(200, payload, headers)
            ):
                with self.assertRaises(RuntimeError):
                    fn("secret")

        for status in (401, 403, 429, 498, 500, 503):
            with self.subTest(status=status), patch.object(
                preflight.requests, "get", return_value=self._response(status)
            ):
                with self.assertRaises(RuntimeError):
                    preflight.check_groq("secret")

    def test_report_schema_v4_is_atomic_and_never_leaks_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            preflight,
            "check_gemini",
            side_effect=requests.ConnectionError("url?key=super-secret"),
        ), patch.object(
            preflight, "check_groq", return_value=preflight.ProviderCheck("groq", "pass", 200)
        ), patch.object(
            preflight, "check_openrouter", return_value=preflight.ProviderCheck("openrouter", "pass", 200)
        ), patch.object(
            preflight, "check_pexels", return_value=self._pexels_pass()
        ), patch.object(
            preflight, "check_pixabay", return_value=preflight.ProviderCheck("pixabay", "pass", 200)
        ):
            output = Path(tmp) / "provider-preflight.json"
            with self.assertRaises(RuntimeError):
                preflight.run_preflight(
                    gemini_key="super-secret",
                    groq_key="b",
                    openrouter_key="c",
                    pexels_key="d",
                    pixabay_key="e",
                    content_model="gemini-2.5-flash",
                    tts_model="gemini-3.1-flash-tts-preview",
                    output=output,
                )
            text = output.read_text(encoding="utf-8")
            payload = json.loads(text)
            self.assertFalse(output.with_name(output.name + ".tmp").exists())
        self.assertEqual(payload["schema_version"], 4)
        self.assertEqual(payload["required_providers"], ["gemini", "pexels"])
        self.assertEqual(payload["fallback_providers"], ["groq", "openrouter", "pixabay"])
        self.assertNotIn("super-secret", text)


if __name__ == "__main__":
    unittest.main()
