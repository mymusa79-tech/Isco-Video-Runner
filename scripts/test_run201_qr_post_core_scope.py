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


class Run201PostCoreQRScopeTests(unittest.TestCase):
    """Regression for Run201's Gold/Short-Cinematic QR lifecycle seam."""

    def setUp(self) -> None:
        self.original_installed = runtime._INSTALLED
        self.original_scanner = security_binding._scan_media_before_vision
        self.original_require = security_binding.require_normal_vision_safe
        self.original_produce = runtime.orchestrator.produce
        runtime._INSTALLED = False
        runtime._RUN184_ACTIVE.set(False)

    def tearDown(self) -> None:
        security_binding._scan_media_before_vision = self.original_scanner
        security_binding.require_normal_vision_safe = self.original_require
        runtime.orchestrator.produce = self.original_produce
        runtime._INSTALLED = self.original_installed
        runtime._RUN184_ACTIVE.set(False)

    def _install_with_fake_gold(self, gold):
        production = SimpleNamespace(run_gold_enforce_phase4=gold)
        with (
            patch.object(runtime, "canonical_runtime_enabled", return_value=True),
            patch(
                "scripts.runtime_reliability.production_entrypoint_modules",
                return_value=[production],
            ),
        ):
            runtime.install_run184_qr_confirmation_runtime()
        return production

    def test_gold_stock_preflight_uses_mature_qr_authority(self) -> None:
        calls: list[str] = []

        def prior_require(scan_result):
            codes = qr._scan_codes(scan_result)
            calls.append("legacy-require:" + ",".join(codes))
            raise RuntimeError("legacy-block:" + ",".join(codes))

        def prior_scanner(_media):
            calls.append("prior-scan")
            # This reproduces Run201: the historical Engine heuristic suspects QR.
            security_binding.require_normal_vision_safe(_scan_result("qr_code_detected"))
            calls.append("prior-scan-complete")

        def gold():
            self.assertTrue(runtime._RUN184_ACTIVE.get())
            security_binding._scan_media_before_vision(Path("gold-short-candidate.mp4"))
            return "gold-ok"

        security_binding._scan_media_before_vision = prior_scanner
        security_binding.require_normal_vision_safe = prior_require
        runtime.orchestrator.produce = lambda: "core-ok"

        with patch.object(
            runtime,
            "_run184_mature_qr_scan",
            side_effect=lambda _media: calls.append("mature-qr"),
        ):
            production = self._install_with_fake_gold(gold)
            self.assertEqual(production.run_gold_enforce_phase4(), "gold-ok")

        self.assertEqual(calls, ["prior-scan", "prior-scan-complete", "mature-qr"])
        self.assertNotIn("legacy-require:qr_code_detected", calls)
        self.assertFalse(runtime._RUN184_ACTIVE.get())

    def test_mature_confirmed_qr_still_blocks_gold_candidate(self) -> None:
        def prior_scanner(_media):
            security_binding.require_normal_vision_safe(_scan_result("qr_code_detected"))

        def prior_require(scan_result):
            codes = qr._scan_codes(scan_result)
            raise RuntimeError("legacy-block:" + ",".join(codes))

        def gold():
            security_binding._scan_media_before_vision(Path("confirmed-qr.mp4"))

        security_binding._scan_media_before_vision = prior_scanner
        security_binding.require_normal_vision_safe = prior_require
        runtime.orchestrator.produce = lambda: "core-ok"

        with patch.object(
            runtime,
            "_run184_mature_qr_scan",
            side_effect=qr._firewall_error("qr_code_detected"),
        ):
            production = self._install_with_fake_gold(gold)
            with self.assertRaisesRegex(RuntimeError, "qr_code_detected"):
                production.run_gold_enforce_phase4()

        self.assertFalse(runtime._RUN184_ACTIVE.get())

    def test_non_qr_security_finding_keeps_historical_authority_in_gold(self) -> None:
        def prior_require(scan_result):
            codes = qr._scan_codes(scan_result)
            raise RuntimeError("legacy-block:" + ",".join(codes))

        def prior_scanner(_media):
            security_binding.require_normal_vision_safe(_scan_result("url_detected"))

        def gold():
            security_binding._scan_media_before_vision(Path("unsafe-url.mp4"))

        security_binding._scan_media_before_vision = prior_scanner
        security_binding.require_normal_vision_safe = prior_require
        runtime.orchestrator.produce = lambda: "core-ok"

        with patch.object(runtime, "_run184_mature_qr_scan") as mature:
            production = self._install_with_fake_gold(gold)
            with self.assertRaisesRegex(RuntimeError, "url_detected"):
                production.run_gold_enforce_phase4()

        mature.assert_not_called()
        self.assertFalse(runtime._RUN184_ACTIVE.get())

    def test_gold_scope_resets_after_exception(self) -> None:
        security_binding._scan_media_before_vision = lambda _media: None
        security_binding.require_normal_vision_safe = lambda _scan_result: None
        runtime.orchestrator.produce = lambda: "core-ok"

        def gold():
            self.assertTrue(runtime._RUN184_ACTIVE.get())
            raise RuntimeError("gold boom")

        production = self._install_with_fake_gold(gold)
        with self.assertRaisesRegex(RuntimeError, "gold boom"):
            production.run_gold_enforce_phase4()
        self.assertFalse(runtime._RUN184_ACTIVE.get())

    def test_noncanonical_install_does_not_patch_historical_gold(self) -> None:
        gold = lambda: "historical"
        production = SimpleNamespace(run_gold_enforce_phase4=gold)
        security_binding._scan_media_before_vision = lambda _media: None
        security_binding.require_normal_vision_safe = lambda _scan_result: None
        runtime.orchestrator.produce = lambda: "core-ok"

        with (
            patch.object(runtime, "canonical_runtime_enabled", return_value=False),
            patch(
                "scripts.runtime_reliability.production_entrypoint_modules",
                return_value=[production],
            ) as modules,
        ):
            runtime.install_run184_qr_confirmation_runtime()

        modules.assert_not_called()
        self.assertIs(production.run_gold_enforce_phase4, gold)
        self.assertEqual(production.run_gold_enforce_phase4(), "historical")
        self.assertFalse(runtime._RUN184_ACTIVE.get())


if __name__ == "__main__":
    unittest.main()
