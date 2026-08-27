from __future__ import annotations

import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from scripts import crossref_reliability as rel


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self._body


def _http_error(code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.crossref.org/works",
        code=code,
        msg="Too Many Requests" if code == 429 else "error",
        hdrs=headers or {},
        fp=None,
    )


class CrossrefRetryTests(unittest.TestCase):
    """Covers the fix for Run #158/#159: a plain HTTPError 429 from Crossref used to
    propagate raw with zero retry/backoff, aborting the whole research attempt even
    after Gemini/OpenRouter had already succeeded."""

    def test_reproduces_the_exact_run_158_159_failure_then_recovers(self):
        error_429 = _http_error(429)
        call_count = {"n": 0}

        def fake_urlopen(request, timeout=25):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise error_429
            return _FakeResponse(b'{"message": {"items": []}}')

        with patch.object(rel.time, "sleep") as sleep:
            raw = rel.fetch_crossref_response(
                urllib.request.Request("https://api.crossref.org/works"),
                urlopen=fake_urlopen,
            )
        self.assertEqual(raw, b'{"message": {"items": []}}')
        self.assertEqual(call_count["n"], 2)
        sleep.assert_called_once()

    def test_bounded_retry_is_exhausted_and_fails_closed(self):
        error_429 = _http_error(429)

        def always_429(request, timeout=25):
            raise error_429

        with patch.object(rel.time, "sleep") as sleep:
            with self.assertRaises(rel.CrossrefRetryExhausted):
                rel.fetch_crossref_response(
                    urllib.request.Request("https://api.crossref.org/works"),
                    urlopen=always_429,
                )
        self.assertEqual(sleep.call_count, rel.MAX_ATTEMPTS - 1)

    def test_non_retryable_failure_is_not_retried_and_raised_unwrapped(self):
        error_404 = _http_error(404)
        calls = {"n": 0}

        def raise_404(request, timeout=25):
            calls["n"] += 1
            raise error_404

        with patch.object(rel.time, "sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                rel.fetch_crossref_response(
                    urllib.request.Request("https://api.crossref.org/works"),
                    urlopen=raise_404,
                )
        self.assertIs(ctx.exception, error_404)
        self.assertEqual(calls["n"], 1)
        sleep.assert_not_called()

    def test_retry_after_header_bounds_the_backoff(self):
        error_429 = _http_error(429, headers={"Retry-After": "2"})
        calls = {"n": 0}

        def fail_once_then_succeed(request, timeout=25):
            calls["n"] += 1
            if calls["n"] == 1:
                raise error_429
            return _FakeResponse(b"{}")

        with patch.object(rel.random, "uniform", return_value=0.0), \
                patch.object(rel.time, "sleep") as sleep:
            rel.fetch_crossref_response(
                urllib.request.Request("https://api.crossref.org/works"),
                urlopen=fail_once_then_succeed,
            )
        sleep.assert_called_once_with(2.0)

    def test_does_not_relax_the_two_source_minimum_it_only_protects_the_http_call(self):
        # This module has no knowledge of _approve's mandatory two-scholarly-source
        # gate; it must not expose anything that could be used to bypass it.
        self.assertNotIn("approve", dir(rel))
        self.assertNotIn("research_pack", dir(rel))


if __name__ == "__main__":
    unittest.main()
