from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import run184_qr_confirmation_closure as qr
from scripts import run184_qr_confirmation_runtime as runtime
from scripts import security_v1_live_binding as security_binding


class Run184QRRuntimeScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_installed = runtime._INSTALLED
        self.original_scanner = security_binding._scan_media_before_vision
        self.original_produce = runtime.orchestrator.produce
        self.original_zxing = qr._zxing_qr_status
        runtime._INSTALLED = False

    def tearDown(self) -> None:
        security_binding._scan_media_before_vision = self.original_scanner
        runtime.orchestrator.produce = self.original_produce
        qr._zxing_qr_status = self.original_zxing
        runtime._INSTALLED = self.original_installed

    def test_outside_produce_delegates_exact_prior_scanner(self) -> None:
        calls: list[tuple[str, str]] = []

        def prior_scanner(media):
            calls.append(("prior", str(media)))

        security_binding._scan_media_before_vision = prior_scanner
        runtime.orchestrator.produce = lambda: "ok"
        runtime.install_run184_qr_confirmation_runtime()

        security_binding._scan_media_before_vision(Path("historical.mp4"))
        self.assertEqual(calls, [("prior", "historical.mp4")])
        self.assertFalse(runtime._RUN184_ACTIVE.get())

    def test_inside_produce_uses_v3_and_scoped_zxing_then_resets(self) -> None:
        calls: list[str] = []

        def prior_scanner(_media):
            calls.append("prior")

        def core_produce():
            self.assertTrue(runtime._RUN184_ACTIVE.get())
            security_binding._scan_media_before_vision(Path("candidate.mp4"))
            self.assertEqual(qr._zxing_qr_status(Path("frame.pgm"), "ZXingReader"), "decoded")
            return "done"

        security_binding._scan_media_before_vision = prior_scanner
        runtime.orchestrator.produce = core_produce
        with patch.object(runtime, "_run184_scan", side_effect=lambda _media: calls.append("v3")), patch.object(
            runtime, "_zxing_qr_status_221", return_value="decoded"
        ) as zxing:
            runtime.install_run184_qr_confirmation_runtime()
            self.assertEqual(runtime.orchestrator.produce(), "done")

        self.assertEqual(calls, ["v3"])
        zxing.assert_called_once()
        self.assertFalse(runtime._RUN184_ACTIVE.get())
        # After the scope closes, the exact historical ZXing owner is visible again.
        with patch.object(self, "original_zxing", return_value="none"):
            pass

    def test_scope_resets_after_production_exception(self) -> None:
        def prior_scanner(_media):
            raise AssertionError("prior scanner must not run inside scope")

        def core_produce():
            self.assertTrue(runtime._RUN184_ACTIVE.get())
            raise RuntimeError("production boom")

        security_binding._scan_media_before_vision = prior_scanner
        runtime.orchestrator.produce = core_produce
        runtime.install_run184_qr_confirmation_runtime()

        with self.assertRaisesRegex(RuntimeError, "production boom"):
            runtime.orchestrator.produce()
        self.assertFalse(runtime._RUN184_ACTIVE.get())

    def test_install_is_idempotent_and_does_not_patch_policy_runtime_owner(self) -> None:
        original_required_runtime = qr._required_runtime
        security_binding._scan_media_before_vision = lambda _media: None
        runtime.orchestrator.produce = lambda: "ok"

        runtime.install_run184_qr_confirmation_runtime()
        first_scanner = security_binding._scan_media_before_vision
        first_produce = runtime.orchestrator.produce
        first_zxing = qr._zxing_qr_status
        runtime.install_run184_qr_confirmation_runtime()

        self.assertIs(security_binding._scan_media_before_vision, first_scanner)
        self.assertIs(runtime.orchestrator.produce, first_produce)
        self.assertIs(qr._zxing_qr_status, first_zxing)
        self.assertIs(qr._required_runtime, original_required_runtime)
        self.assertTrue(getattr(first_scanner, "_isco_run184_qr_dispatcher", False))
        self.assertTrue(getattr(first_produce, "_isco_run184_qr_scope", False))
        self.assertTrue(getattr(first_zxing, "_isco_run184_zxing_dispatcher", False))


if __name__ == "__main__":
    unittest.main()
