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
        response.headers = dict(headers or {})
        return response

    def _capacity_headers(self, *, remaining: int = 100, limit: int = 200, reset: str = "1787684000") -> dict:
        return {
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": reset,
        }

    def test_gemini_requires_both_configured_model_capabilities(self) -> None:
        payload = {"models": [{"name": "models/gemini-3.5-flash-lite"}, {"name": "models/gemini-3.1-flash-tts-preview"}]}
        with patch.object(preflight.requests, "get", return_value=self._response(200, payload)) as get:
            result = preflight.check_gemini("secret", content_model="gemini-2.5-flash", tts_model="gemini-3.1-flash-tts-preview")
        self.assertEqual(result.status, "pass")
        self.assertEqual(get.call_args.kwargs["headers"]["x-goog-api-key"], "secret")
        self.assertEqual(get.call_args.kwargs["params"]["pageSize"], 1000)

    def test_gemini_pagination_is_followed_before_declaring_model_missing(self) -> None:
        first = self._response(200, {"models": [{"name": "models/gemini-3.5-flash-lite"}], "nextPageToken": "p2"})
        second = self._response(200, {"models": [{"name": "models/gemini-3.1-flash-tts-preview"}]})
        with patch.object(preflight.requests, "get", side_effect=[first, second]) as get:
            result = preflight.check_gemini("secret", content_model="gemini-2.5-flash", tts_model="gemini-3.1-flash-tts-preview")
        self.assertEqual(result.status, "pass")
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.kwargs["params"]["pageToken"], "p2")

    def test_gemini_repeated_page_token_blocks(self) -> None:
        response = self._response(200, {"models": [], "nextPageToken": "same"})
        with patch.object(preflight.requests, "get", side_effect=[response, response]):
            with self.assertRaisesRegex(RuntimeError, "repeated"):
                preflight.check_gemini("secret", content_model="gemini-2.5-flash", tts_model="gemini-3.1-flash-tts-preview")

    def test_gemini_missing_tts_model_blocks_before_production(self) -> None:
        payload = {"models": [{"name": "models/gemini-3.5-flash-lite"}]}
        with patch.object(preflight.requests, "get", return_value=self._response(200, payload)):
            with self.assertRaisesRegex(RuntimeError, "configured model unavailable"):
                preflight.check_gemini("secret", content_model="gemini-2.5-flash", tts_model="gemini-3.1-flash-tts-preview")

    def test_openrouter_uses_documented_key_endpoint(self) -> None:
        with patch.object(preflight.requests, "get", return_value=self._response(200, {"data": {"limit": None}})) as get:
            result = preflight.check_openrouter("secret")
        self.assertEqual(result.status, "pass")
        self.assertEqual(get.call_args.args[0], preflight.OPENROUTER_KEY_URL)
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer secret")

    def test_pexels_requires_current_endpoint_and_capacity_headroom(self) -> None:
        response = self._response(200, {"videos": []}, self._capacity_headers(remaining=87))
        with patch.object(preflight.requests, "get", return_value=response) as get:
            result = preflight.check_pexels("secret")
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.rate_limit_remaining, 87)
        self.assertEqual(result.required_headroom, preflight.STOCK_REQUIRED_HEADROOM)
        self.assertTrue(result.headroom_ok)
        self.assertEqual(get.call_args.args[0], "https://api.pexels.com/v1/videos/search")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "secret")

    def test_pixabay_requires_capacity_headroom_case_insensitively(self) -> None:
        headers = {
            "x-ratelimit-limit": "100",
            "x-ratelimit-remaining": "75",
            "x-ratelimit-reset": "60",
        }
        response = self._response(200, {"hits": []}, headers)
        with patch.object(preflight.requests, "get", return_value=response):
            result = preflight.check_pixabay("secret")
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.rate_limit_limit, 100)
        self.assertEqual(result.rate_limit_remaining, 75)
        self.assertEqual(result.rate_limit_reset, "60")

    def test_stock_provider_low_or_missing_headroom_blocks_before_production(self) -> None:
        cases = [
            (self._capacity_headers(remaining=preflight.STOCK_REQUIRED_HEADROOM - 1), "headroom too low"),
            ({}, "could not prove"),
        ]
        for headers, message in cases:
            with self.subTest(message=message):
                response = self._response(200, {"videos": []}, headers)
                with patch.object(preflight.requests, "get", return_value=response):
                    with self.assertRaisesRegex(RuntimeError, message):
                        preflight.check_pexels("secret")

    def test_success_http_with_malformed_schema_still_blocks(self) -> None:
        cases = [
            (preflight.check_groq, {"unexpected": []}),
            (preflight.check_openrouter, {"data": []}),
            (preflight.check_pexels, {"videos": {}}),
            (preflight.check_pixabay, {"hits": {}}),
        ]
        for fn, payload in cases:
            with self.subTest(fn=fn.__name__):
                with patch.object(preflight.requests, "get", return_value=self._response(200, payload, self._capacity_headers())):
                    with self.assertRaises(RuntimeError):
                        fn("secret")

    def test_auth_rate_and_server_failures_are_blocking(self) -> None:
        for status in (401, 403, 429, 500, 503):
            with self.subTest(status=status):
                with patch.object(preflight.requests, "get", return_value=self._response(status)):
                    with self.assertRaises(RuntimeError):
                        preflight.check_groq("secret")

    def test_full_preflight_checks_all_five_and_writes_atomic_report(self) -> None:
        checks = [
            preflight.ProviderCheck("gemini", "pass", 200),
            preflight.ProviderCheck("groq", "pass", 200),
            preflight.ProviderCheck("openrouter", "pass", 200),
            preflight.ProviderCheck("pexels", "pass", 200, rate_limit_remaining=100, required_headroom=24, headroom_ok=True),
            preflight.ProviderCheck("pixabay", "pass", 200, rate_limit_remaining=100, required_headroom=24, headroom_ok=True),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "provider-preflight.json"
            with patch.object(preflight, "check_gemini", return_value=checks[0]), patch.object(preflight, "check_groq", return_value=checks[1]), patch.object(preflight, "check_openrouter", return_value=checks[2]), patch.object(preflight, "check_pexels", return_value=checks[3]), patch.object(preflight, "check_pixabay", return_value=checks[4]):
                result = preflight.run_preflight(
                    gemini_key="a", groq_key="b", openrouter_key="c", pexels_key="d", pixabay_key="e",
                    content_model="gemini-2.5-flash", tts_model="gemini-3.1-flash-tts-preview", output=output,
                )
            self.assertEqual([item.provider for item in result], ["gemini", "groq", "openrouter", "pexels", "pixabay"])
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 3)
            self.assertEqual(len(payload["checks"]), 5)
            self.assertFalse(output.with_name(output.name + ".tmp").exists())

    def test_report_never_contains_secret_on_network_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "provider-preflight.json"
            with patch.object(preflight, "check_gemini", side_effect=requests.ConnectionError("url?key=super-secret")), patch.object(preflight, "check_groq", return_value=preflight.ProviderCheck("groq", "pass", 200)), patch.object(preflight, "check_openrouter", return_value=preflight.ProviderCheck("openrouter", "pass", 200)), patch.object(preflight, "check_pexels", return_value=preflight.ProviderCheck("pexels", "pass", 200)), patch.object(preflight, "check_pixabay", return_value=preflight.ProviderCheck("pixabay", "pass", 200)):
                with self.assertRaises(RuntimeError):
                    preflight.run_preflight(
                        gemini_key="super-secret", groq_key="b", openrouter_key="c", pexels_key="d", pixabay_key="e",
                        content_model="gemini-2.5-flash", tts_model="gemini-3.1-flash-tts-preview", output=output,
                    )
            self.assertNotIn("super-secret", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
