from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2 import ai_still


class _Response:
    def __init__(self, payload: dict, *, status: int = 200) -> None:
        self._raw = json.dumps(payload).encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _amount: int = -1) -> bytes:
        return self._raw


class CloudflareAIStillTests(unittest.TestCase):
    def test_zero_cost_preflight_rejects_paid_or_unknown_active_plan(self) -> None:
        self.assertTrue(
            ai_still._subscription_is_proven_free(
                {"state": "Provisioned", "price": 0, "rate_plan": {"id": "free"}}
            )
        )
        self.assertFalse(
            ai_still._subscription_is_proven_free(
                {"state": "Paid", "price": 5, "rate_plan": {"id": "pro"}}
            )
        )
        with mock.patch.object(
            ai_still.urllib.request,
            "urlopen",
            return_value=_Response(
                {
                    "success": True,
                    "result": [
                        {"state": "Paid", "price": 5, "rate_plan": {"id": "pro"}}
                    ],
                }
            ),
        ), self.assertRaisesRegex(
            ai_still.CloudflareAIStillUnavailable,
            "paid_or_unknown",
        ):
            ai_still._prove_workers_free("token", "a" * 32)

    def test_reference_type_comes_from_bytes_not_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reference = Path(temporary) / "reference.jpg"
            reference.write_bytes(b"\x89PNG\r\n\x1a\n" + b"P" * 2048)
            name, raw, content_type = ai_still._reference_part(reference)
        self.assertEqual(name, "reference.jpg")
        self.assertTrue(raw.startswith(b"\x89PNG"))
        self.assertEqual(content_type, "image/png")

    def test_generation_uses_flux_multipart_and_records_provenance(self) -> None:
        raw_image = b"\xff\xd8\xff" + b"J" * 2048
        response = _Response(
            {
                "success": True,
                "result": {"image": base64.b64encode(raw_image).decode("ascii")},
            }
        )
        seen = {}

        def urlopen(request, *, timeout):
            seen.update(request=request, timeout=timeout)
            return response

        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ,
            {
                "CLEAN_V2_AI_STILL_FREE_ONLY": "true",
                "CLOUDFLARE_API_TOKEN": "token",
                "CLOUDFLARE_ACCOUNT_ID": "a" * 32,
            },
            clear=False,
        ), mock.patch.object(
            ai_still, "_prove_workers_free"
        ) as prove_free, mock.patch.object(
            ai_still.urllib.request, "urlopen", side_effect=urlopen
        ):
            destination = Path(temporary) / "still.image"
            provenance = ai_still.generate_cloudflare_ai_still(
                prompt="A coherent warm notebook environment without faces",
                destination=destination,
                fmt="short",
            )
            generated = destination.read_bytes()

        self.assertEqual(generated, raw_image)
        prove_free.assert_called_once_with("token", "a" * 32)
        self.assertIn(ai_still.CLOUDFLARE_IMAGE_MODEL, seen["request"].full_url)
        self.assertIn(
            "multipart/form-data",
            seen["request"].get_header("Content-type"),
        )
        self.assertIn(b'name="prompt"', seen["request"].data)
        self.assertIn(b"720", seen["request"].data)
        self.assertEqual(provenance["content_type"], "image/jpeg")
        self.assertEqual(provenance["height"], 1280)
        self.assertTrue(provenance["ai_generated"])

    def test_disabled_feature_never_reaches_credentials_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ,
            {"CLEAN_V2_AI_STILL_FREE_ONLY": "false"},
            clear=False,
        ), mock.patch.object(ai_still, "_credentials") as credentials:
            with self.assertRaisesRegex(
                ai_still.CloudflareAIStillUnavailable,
                "feature_flag_disabled",
            ):
                ai_still.generate_cloudflare_ai_still(
                    prompt="valid prompt",
                    destination=Path(temporary) / "still.image",
                    fmt="film",
                )
        credentials.assert_not_called()


if __name__ == "__main__":
    unittest.main()
