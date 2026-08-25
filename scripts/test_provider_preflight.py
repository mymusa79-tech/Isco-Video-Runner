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

    def _gemini_models(self, *, next_token: str | None = None) -> Mock:
        payload = {
            "models": [
                {"name": "models/gemini-3.5-flash-lite"},
                {"name": "models/gemini-3.1-flash-tts-preview"},
            ]
        }
        if next_token:
            payload["nextPageToken"] = next_token
        return self._response(200, payload)

    def _gemini_tokens(self) -> Mock:
        return self._response(200, {"totalTokens": 3})

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

    def test_gemini_requires_both_models_and_zero_inference_request_path(self) -> None:
        with patch.object(preflight.requests, "get", return_value=self._gemini_models()) as get, patch.object(
            preflight.requests, "post", return_value=self._gemini_tokens()
        ) as post:
            result = preflight.check_gemini(
                "secret",
                content_model="gemini-2.5-flash",
                tts_model="gemini-3.1-flash-tts-preview",
            )
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.capacity_status, "dynamic_unobservable")
        self.assertEqual(get.call_args.kwargs["headers"]["x-goog-api-key"], "secret")
        self.assertEqual(get.call_args.kwargs["params"]["pageSize"], 1000)
        self.assertIn("gemini-3.5-flash-lite:countTokens", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "secret")

    def test_gemini_pagination_is_followed_before_probe(self) -> None:
        first = self._response(
            200,
            {"models": [{"name": "models/gemini-3.5-flash-lite"}], "nextPageToken": "p2"},
        )
        second = self._response(200, {"models": [{"name": "models/gemini-3.1-flash-tts-preview"}]})
        with patch.object(preflight.requests, "get", side_effect=[first, second]) as get, patch.object(
            preflight.requests, "post", return_value=self._gemini_tokens()
        ) as post:
            result = preflight.check_gemini(
                "secret",
                content_model="gemini-2.5-flash",
                tts_model="gemini-3.1-flash-tts-preview",
            )
        self.assertEqual(result.status, "pass")
        self.assertEqual(get.call_count, 2)
        self.assertEqual(post.call_count, 1)

    def test_gemini_repeated_page_token_blocks_before_capacity_probe(self) -> None:
        response = self._response(200, {"models": [], "nextPageToken": "same"})
        with patch.object(preflight.requests, "get", side_effect=[response, response]), patch.object(
            preflight.requests, "post"
        ) as post:
            with self.assertRaisesRegex(RuntimeError, "repeated"):
                preflight.check_gemini(
                    "secret",
                    content_model="gemini-2.5-flash",
                    tts_model="gemini-3.1-flash-tts-preview",
                )
        post.assert_not_called()

    def test_gemini_missing_tts_model_blocks_before_capacity_probe(self) -> None:
        payload = {"models": [{"name": "models/gemini-3.5-flash-lite"}]}
        with patch.object(preflight.requests, "get", return_value=self._response(200, payload)), patch.object(
            preflight.requests, "post"
        ) as post:
            with self.assertRaisesRegex(RuntimeError, "configured model unavailable"):
                preflight.check_gemini(
                    "secret",
                    content_model="gemini-2.5-flash",
                    tts_model="gemini-3.1-flash-tts-preview",
                )
        post.assert_not_called()

    def test_gemini_zero_inference_probe_malformed_response_blocks(self) -> None:
        with patch.object(preflight.requests, "get", return_value=self._gemini_models()), patch.object(
            preflight.requests, "post", return_value=self._response(200, {"totalTokens": 0})
        ):
            with self.assertRaisesRegex(RuntimeError, "countTokens"):
                preflight.check_gemini(
                    "secret",
                    content_model="gemini-2.5-flash",
                    tts_model="gemini-3.1-flash-tts-preview",
                )

    def test_groq_requires_positive_request_and_token_headroom(self) -> None:
        response = self._response(200, {"data": []}, self._groq_headers())
        with patch.object(preflight.requests, "get", return_value=response):
            result = preflight.check_groq("secret")
        self.assertEqual(result.capacity_status, "positive")
        self.assertEqual(result.capacity_remaining, 100)
        self.assertEqual(result.capacity_unit, "requests")

        exhausted_requests = self._response(200, {"data": []}, self._groq_headers(requests_left="0"))
        with patch.object(preflight.requests, "get", return_value=exhausted_requests):
            with self.assertRaisesRegex(RuntimeError, "no remaining requests"):
                preflight.check_groq("secret")

        exhausted_tokens = self._response(200, {"data": []}, self._groq_headers(tokens_left="0"))
        with patch.object(preflight.requests, "get", return_value=exhausted_tokens):
            with self.assertRaisesRegex(RuntimeError, "no remaining token"):
                preflight.check_groq("secret")

    def test_groq_missing_capacity_headers_fails_closed(self) -> None:
        with patch.object(preflight.requests, "get", return_value=self._response(200, {"data": []})):
            with self.assertRaisesRegex(RuntimeError, "capacity evidence missing"):
                preflight.check_groq("secret")

    def test_openrouter_uses_key_capacity_and_rejects_admin_or_exhausted_key(self) -> None:
        healthy = self._response(
            200,
            {"data": {"limit": 100, "limit_remaining": 74.5, "limit_reset": "monthly"}},
        )
        with patch.object(preflight.requests, "get", return_value=healthy) as get:
            result = preflight.check_openrouter("secret")
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.capacity_status, "positive")
        self.assertEqual(result.capacity_remaining, 74.5)
        self.assertEqual(get.call_args.args[0], preflight.OPENROUTER_KEY_URL)
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer secret")

        unlimited = self._response(200, {"data": {"limit": None}})
        with patch.object(preflight.requests, "get", return_value=unlimited):
            result = preflight.check_openrouter("secret")
        self.assertEqual(result.capacity_status, "unbounded_key_limit")

        exhausted = self._response(200, {"data": {"limit": 10, "limit_remaining": 0}})
        with patch.object(preflight.requests, "get", return_value=exhausted):
            with self.assertRaisesRegex(RuntimeError, "exhausted"):
                preflight.check_openrouter("secret")

        admin = self._response(200, {"data": {"limit": None, "is_management_key": True}})
        with patch.object(preflight.requests, "get", return_value=admin):
            with self.assertRaisesRegex(RuntimeError, "administrative"):
                preflight.check_openrouter("secret")

    def test_openrouter_expired_key_blocks(self) -> None:
        response = self._response(200, {"data": {"limit": None, "expires_at": "2020-01-01T00:00:00Z"}})
        with patch.object(preflight.requests, "get", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "expired"):
                preflight.check_openrouter("secret")

    def test_pexels_requires_documented_positive_monthly_headroom(self) -> None:
        response = self._response(200, {"videos": []}, self._pexels_headers(remaining="123"))
        with patch.object(preflight.requests, "get", return_value=response) as get:
            result = preflight.check_pexels("secret")
        self.assertEqual(result.capacity_remaining, 123)
        self.assertEqual(result.capacity_unit, "monthly requests")
        self.assertEqual(get.call_args.args[0], "https://api.pexels.com/v1/videos/search")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "secret")

        exhausted = self._response(200, {"videos": []}, self._pexels_headers(remaining="0"))
        with patch.object(preflight.requests, "get", return_value=exhausted):
            with self.assertRaisesRegex(RuntimeError, "no remaining monthly requests"):
                preflight.check_pexels("secret")

    def test_pixabay_requires_documented_positive_window_headroom(self) -> None:
        response = self._response(200, {"hits": []}, self._pixabay_headers(remaining="80"))
        with patch.object(preflight.requests, "get", return_value=response):
            result = preflight.check_pixabay("secret")
        self.assertEqual(result.capacity_remaining, 80)
        self.assertEqual(result.capacity_unit, "requests/60s window")

        exhausted = self._response(200, {"hits": []}, self._pixabay_headers(remaining="0"))
        with patch.object(preflight.requests, "get", return_value=exhausted):
            with self.assertRaisesRegex(RuntimeError, "no remaining requests"):
                preflight.check_pixabay("secret")

    def test_success_http_with_malformed_schema_still_blocks(self) -> None:
        cases = [
            (preflight.check_groq, {"unexpected": []}, self._groq_headers()),
            (preflight.check_openrouter, {"data": []}, {}),
            (preflight.check_pexels, {"videos": {}}, self._pexels_headers()),
            (preflight.check_pixabay, {"hits": {}}, self._pixabay_headers()),
        ]
        for fn, payload, headers in cases:
            with self.subTest(fn=fn.__name__):
                with patch.object(preflight.requests, "get", return_value=self._response(200, payload, headers)):
                    with self.assertRaises(RuntimeError):
                        fn("secret")

    def test_auth_rate_capacity_and_server_failures_are_blocking(self) -> None:
        for status in (401, 403, 429, 498, 500, 503):
            with self.subTest(status=status):
                with patch.object(preflight.requests, "get", return_value=self._response(status)):
                    with self.assertRaises(RuntimeError):
                        preflight.check_groq("secret")

    def test_full_preflight_writes_atomic_capacity_report_v3(self) -> None:
        checks = [
            preflight.ProviderCheck("gemini", "pass", 200, capacity_status="dynamic_unobservable"),
            preflight.ProviderCheck("groq", "pass", 200, capacity_status="positive", capacity_remaining=100),
            preflight.ProviderCheck("openrouter", "pass", 200, capacity_status="positive", capacity_remaining=20.0),
            preflight.ProviderCheck("pexels", "pass", 200, capacity_status="positive", capacity_remaining=100),
            preflight.ProviderCheck("pixabay", "pass", 200, capacity_status="positive", capacity_remaining=90),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "provider-preflight.json"
            with patch.object(preflight, "check_gemini", return_value=checks[0]), patch.object(
                preflight, "check_groq", return_value=checks[1]
            ), patch.object(preflight, "check_openrouter", return_value=checks[2]), patch.object(
                preflight, "check_pexels", return_value=checks[3]
            ), patch.object(preflight, "check_pixabay", return_value=checks[4]):
                result = preflight.run_preflight(
                    gemini_key="a",
                    groq_key="b",
                    openrouter_key="c",
                    pexels_key="d",
                    pixabay_key="e",
                    content_model="gemini-2.5-flash",
                    tts_model="gemini-3.1-flash-tts-preview",
                    output=output,
                )
            self.assertEqual(
                [item.provider for item in result],
                ["gemini", "groq", "openrouter", "pexels", "pixabay"],
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 3)
            self.assertIn("capacity_contract", payload)
            self.assertEqual(len(payload["checks"]), 5)
            self.assertFalse(output.with_name(output.name + ".tmp").exists())

    def test_report_never_contains_secret_on_network_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "provider-preflight.json"
            with patch.object(
                preflight,
                "check_gemini",
                side_effect=requests.ConnectionError("url?key=super-secret"),
            ), patch.object(
                preflight,
                "check_groq",
                return_value=preflight.ProviderCheck("groq", "pass", 200),
            ), patch.object(
                preflight,
                "check_openrouter",
                return_value=preflight.ProviderCheck("openrouter", "pass", 200),
            ), patch.object(
                preflight,
                "check_pexels",
                return_value=preflight.ProviderCheck("pexels", "pass", 200),
            ), patch.object(
                preflight,
                "check_pixabay",
                return_value=preflight.ProviderCheck("pixabay", "pass", 200),
            ):
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
            self.assertNotIn("super-secret", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
