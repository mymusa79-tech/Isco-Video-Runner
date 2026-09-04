from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import run_control_production as control
from scripts.short_finishing_capabilities import ShortFinishingCapabilities


class ControlProductionTimeoutTests(unittest.TestCase):
    def _request(self) -> dict:
        request = {
            "schema_version": 1,
            "request_id": "req-parent-s1",
            "parent_control_request_id": "req-parent",
            "source": "telegram_editorial_control_panel",
            "kind": "short",
            "approval_scope": "short_sibling",
            "approved_by_user": True,
            "approved_topic": "زاوية قصيرة",
            "status": "approved_waiting_production_activation",
            "production_dispatch_authorized": False,
        }
        request["request_sha256"] = control._canonical_request_hash(request)
        return request

    def _capabilities(self) -> ShortFinishingCapabilities:
        return ShortFinishingCapabilities(
            gemini="test-owned-gemini",
            pexels="test-owned-pexels",
            pixabay="test-owned-pixabay",
        )

    def test_child_subprocess_has_hard_timeout_and_fails_closed(self) -> None:
        request = self._request()
        with tempfile.TemporaryDirectory() as td, patch.object(
            control, "_output_dirs", return_value=set()
        ), patch.object(
            control.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["python", "child"], timeout=control.CONTROL_CHILD_TIMEOUT_SECONDS),
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                control.execute_child_subprocess(
                    request,
                    runtime_root=Path(td),
                    capabilities=self._capabilities(),
                )
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["timeout"], control.CONTROL_CHILD_TIMEOUT_SECONDS)
        self.assertTrue(run.call_args.kwargs["check"])


if __name__ == "__main__":
    unittest.main()
