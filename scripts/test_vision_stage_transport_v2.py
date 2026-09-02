from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from scripts import vision_stage_contract_v2 as contract
from scripts import vision_stage_transport_v2 as transport


def _preview(temp_dir: str) -> Path:
    path = Path(temp_dir) / "preview.mp4"
    path.write_bytes(b"fake-preview")
    return path


class VisionStageTransportV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        transport._install_transport_boundary()

    def test_raw_requests_timeout_becomes_provider_transient_stage_error(self) -> None:
        original = getattr(contract._openrouter_call, "_isco_vision_transport_original", None)
        self.assertIsNotNone(original)
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            original.__globals__["requests"], "post", side_effect=requests.Timeout("socket read timeout")
        ), mock.patch.object(
            original.__globals__["legacy"], "_sample_preview_frames", return_value=[b"a", b"b", b"c"]
        ), mock.patch.object(
            original.__globals__["legacy"], "_openrouter_key", return_value="test-key"
        ):
            with self.assertRaises(contract.VisionStageError) as raised:
                contract._openrouter_call(
                    _preview(temp_dir),
                    narration_context="ctx",
                    intended_visual="intent",
                    model="openrouter/free",
                )
        self.assertEqual(raised.exception.code, contract.VisionErrorCode.PROVIDER_TRANSIENT)
        self.assertIn("transport timeout", str(raised.exception))

    def test_raw_connection_error_becomes_provider_transient_stage_error(self) -> None:
        original = getattr(contract._openrouter_call, "_isco_vision_transport_original", None)
        self.assertIsNotNone(original)
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            original.__globals__["requests"], "post", side_effect=requests.ConnectionError("reset")
        ), mock.patch.object(
            original.__globals__["legacy"], "_sample_preview_frames", return_value=[b"a", b"b", b"c"]
        ), mock.patch.object(
            original.__globals__["legacy"], "_openrouter_key", return_value="test-key"
        ):
            with self.assertRaises(contract.VisionStageError) as raised:
                contract._openrouter_call(
                    _preview(temp_dir),
                    narration_context="ctx",
                    intended_visual="intent",
                    model="openrouter/free",
                )
        self.assertEqual(raised.exception.code, contract.VisionErrorCode.PROVIDER_TRANSIENT)
        self.assertIn("connection failure", str(raised.exception))

    def test_402_balance_requirement_is_capacity(self) -> None:
        self.assertEqual(
            transport._classify_http(402, "This request requires at least $1.00 in balance"),
            contract.VisionErrorCode.CAPACITY,
        )

    def test_non_auth_403_is_capacity(self) -> None:
        self.assertEqual(
            transport._classify_http(403, "Provider disabled this route for policy/capacity reasons"),
            contract.VisionErrorCode.CAPACITY,
        )

    def test_auth_403_is_auth_config(self) -> None:
        self.assertEqual(
            transport._classify_http(403, "Invalid API key permission"),
            contract.VisionErrorCode.AUTH_CONFIG,
        )

    def test_transport_installer_is_idempotent(self) -> None:
        once = contract._openrouter_call
        transport._install_transport_boundary()
        self.assertIs(contract._openrouter_call, once)


if __name__ == "__main__":
    unittest.main()
