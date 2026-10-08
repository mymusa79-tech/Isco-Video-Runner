import json
import unittest
import urllib.error
from io import BytesIO
from unittest.mock import patch

from clean_v2 import mistral_executor as mx


def _http_error(status, body):
    return urllib.error.HTTPError(
        "https://api.mistral.ai", status, "err", {}, BytesIO(json.dumps(body).encode())
    )


class Mistral429DiagnosticTests(unittest.TestCase):
    def test_error_body_is_recorded_bounded_and_without_prompt(self):
        mx.reset_mistral_executor_telemetry()
        body = {"object": "error", "type": "rate_limit", "code": "1300", "message": "x" * 900}
        with patch.object(mx, "_read_secret", return_value="k"), patch(
            "urllib.request.urlopen", side_effect=_http_error(429, body)
        ):
            with self.assertRaises(mx.MistralExecutorWireFailure) as ctx:
                mx.mistral_executor_json("SECRET PROMPT", max_tokens=10, task_kind="planning")
        self.assertEqual(ctx.exception.reason_code, "http_429")
        entry = mx.get_mistral_executor_telemetry()[-1]
        self.assertEqual(entry["error"]["type"], "rate_limit")
        self.assertEqual(len(entry["error"]["message"]), 300)
        self.assertNotIn("SECRET PROMPT", json.dumps(entry))

    def test_unparseable_error_body_adds_no_error_field(self):
        mx.reset_mistral_executor_telemetry()
        exc = urllib.error.HTTPError("u", 429, "e", {}, BytesIO(b"<html>"))
        with patch.object(mx, "_read_secret", return_value="k"), patch(
            "urllib.request.urlopen", side_effect=exc
        ):
            with self.assertRaises(mx.MistralExecutorWireFailure):
                mx.mistral_executor_json("p", max_tokens=10, task_kind="planning")
        self.assertNotIn("error", mx.get_mistral_executor_telemetry()[-1])


if __name__ == "__main__":
    unittest.main()
