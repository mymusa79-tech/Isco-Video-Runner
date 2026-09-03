from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import run184_qr_confirmation_closure as qr
from scripts import run184_qr_confirmation_runtime as runtime
from scripts import security_v1_live_binding as security_binding


def _scan_result(*codes: str):
    return SimpleNamespace(
        detections=tuple(SimpleNamespace(code=code) for code in codes),
        safe_for_normal_vision=not bool(codes),
    )


class Run184QRRuntimeScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_installed = runtime._INSTALLED
        self.original_scanner = security_binding._scan_media_before_vision
        self.original_require = security_binding.require_normal_vision_safe
        self.original_produce = runtime.orchestrator.produce
        runtime._INSTALLED = False

    def tearDown(self) -> None:
        security_binding._scan_media_before_vision = self.original_scanner
        security_binding.require_normal_vision_safe = self.original_require
        runtime.orchestrator.produce = self.original_produce
        runtime._INSTALLED = self.original_installed

    def test_outside_produce_delegates_exact_prior_scanner_and_require(self) -> None:
        calls: list[tuple[str, str]] = []

        def prior_scanner(media):
            calls.append(("prior-scan", str(media)))

        def prior_require(_scan_result):
            calls.append(("prior-require", "called"))

        security_binding._scan_media_before_vision = prior_scanner
        security_binding.require_normal_vision_safe = prior_require
        runtime.orchestrator.produce = lambda: "ok"
        runtime.install_run184_qr_confirmation_runtime()

        security_binding._scan_media_before_vision(Path("historical.mp4"))
        security_binding.require_normal_vision_safe(_scan_result("qr_code_detected"))
        self.assertEqual(
            calls,
            [("prior-scan", "historical.mp4"), ("prior-require", "called")],
        )
        self.assertFalse(runtime._RUN184_ACTIVE.get())

    def test_inside_produce_runs_historical_scan_before_mature_qr_scan(self) -> None:
        calls: list[str] = []

        def prior_require(_scan_result):
            calls.append("legacy-require")

        def prior_scanner(_media):
            calls.append("prior-scan")
            # QR-only suspicion must not terminate the certified scanner.
            security_binding.require_normal_vision_safe(_scan_result("qr_code_detected"))
            calls.append("prior-scan-complete")

        def core_produce():
            self.assertTrue(runtime._RUN184_ACTIVE.get())
            security_binding._scan_media_before_vision(Path("candidate.mp4"))
            return "done"

        security_binding._scan_media_before_vision = prior_scanner
        security_binding.require_normal_vision_safe = prior_require
        runtime.orchestrator.produce = core_produce
        with patch.object(
            runtime,
            "_run184_mature_qr_scan",
            side_effect=lambda _media: calls.append("mature-qr"),
        ):
            runtime.install_run184_qr_confirmation_runtime()
            self.assertEqual(runtime.orchestrator.produce(), "done")

        self.assertEqual(calls, ["prior-scan", "prior-scan-complete", "mature-qr"])
        self.assertFalse(runtime._RUN184_ACTIVE.get())

    def test_mixed_or_non_qr_findings_keep_historical_blocking_authority(self) -> None:
        calls: list[str] = []

        def prior_require(scan_result):
            calls.append("legacy-require")
            codes = qr._scan_codes(scan_result)
            raise RuntimeError("legacy-block:" + ",".join(codes))

        def prior_scanner(_media):
            security_binding.require_normal_vision_safe(
                _scan_result("qr_code_detected", "prompt_like_text_detected")
            )

        def core_produce():
            security_binding._scan_media_before_vision(Path("candidate.mp4"))

        security_binding._scan_media_before_vision = prior_scanner
        security_binding.require_normal_vision_safe = prior_require
        runtime.orchestrator.produce = core_produce
        with patch.object(runtime, "_run184_mature_qr_scan") as mature:
            runtime.install_run184_qr_confirmation_runtime()
            with self.assertRaisesRegex(RuntimeError, "prompt_like_text_detected"):
                runtime.orchestrator.produce()

        self.assertEqual(calls, ["legacy-require"])
        mature.assert_not_called()
        self.assertFalse(runtime._RUN184_ACTIVE.get())

    def test_qr_only_suppression_does_not_hide_later_non_qr_frame(self) -> None:
        calls: list[str] = []

        def prior_require(scan_result):
            codes = qr._scan_codes(scan_result)
            calls.append("legacy:" + ",".join(codes))
            raise RuntimeError("legacy-block:" + ",".join(codes))

        def prior_scanner(_media):
            # Simulate an early false QR suspicion followed by a later real non-QR
            # security finding. The first must be deferred so the second frame is seen.
            security_binding.require_normal_vision_safe(_scan_result("qr_code_detected"))
            calls.append("reached-later-frame")
            security_binding.require_normal_vision_safe(_scan_result("url_detected"))

        security_binding._scan_media_before_vision = prior_scanner
        security_binding.require_normal_vision_safe = prior_require
        runtime.orchestrator.produce = lambda: security_binding._scan_media_before_vision(
            Path("candidate.mp4")
        )
        with patch.object(runtime, "_run184_mature_qr_scan") as mature:
            runtime.install_run184_qr_confirmation_runtime()
            with self.assertRaisesRegex(RuntimeError, "url_detected"):
                runtime.orchestrator.produce()

        self.assertEqual(calls, ["reached-later-frame", "legacy:url_detected"])
        mature.assert_not_called()

    def test_scope_resets_after_production_exception(self) -> None:
        security_binding._scan_media_before_vision = lambda _media: None
        security_binding.require_normal_vision_safe = lambda _scan_result: None

        def core_produce():
            self.assertTrue(runtime._RUN184_ACTIVE.get())
            raise RuntimeError("production boom")

        runtime.orchestrator.produce = core_produce
        runtime.install_run184_qr_confirmation_runtime()

        with self.assertRaisesRegex(RuntimeError, "production boom"):
            runtime.orchestrator.produce()
        self.assertFalse(runtime._RUN184_ACTIVE.get())

    def test_install_is_idempotent_and_does_not_patch_policy_runtime_owner(self) -> None:
        original_required_runtime = qr._required_runtime
        security_binding._scan_media_before_vision = lambda _media: None
        security_binding.require_normal_vision_safe = lambda _scan_result: None
        runtime.orchestrator.produce = lambda: "ok"

        runtime.install_run184_qr_confirmation_runtime()
        first_scanner = security_binding._scan_media_before_vision
        first_require = security_binding.require_normal_vision_safe
        first_produce = runtime.orchestrator.produce
        runtime.install_run184_qr_confirmation_runtime()

        self.assertIs(security_binding._scan_media_before_vision, first_scanner)
        self.assertIs(security_binding.require_normal_vision_safe, first_require)
        self.assertIs(runtime.orchestrator.produce, first_produce)
        self.assertIs(qr._required_runtime, original_required_runtime)
        self.assertTrue(getattr(first_scanner, "_isco_run184_qr_dispatcher", False))
        self.assertTrue(getattr(first_require, "_isco_run184_qr_require_dispatcher", False))
        self.assertTrue(getattr(first_produce, "_isco_run184_qr_scope", False))


if __name__ == "__main__":
    unittest.main()
