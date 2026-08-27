from __future__ import annotations

import unittest
from unittest.mock import Mock

from scripts import run123_runtime_latency_guard as guard


class Run123RuntimeLatencyGuardTests(unittest.TestCase):
    def test_media_and_sibling_limits_are_finite_and_not_aggressive(self) -> None:
        self.assertEqual(guard.FFMPEG_COMMAND_TIMEOUT_SECONDS, 900)
        self.assertEqual(guard.FFPROBE_COMMAND_TIMEOUT_SECONDS, 120)
        self.assertEqual(guard.SIBLING_SHORT_CHILD_TIMEOUT_SECONDS, 600)
        self.assertGreater(guard.FFMPEG_COMMAND_TIMEOUT_SECONDS, guard.FFPROBE_COMMAND_TIMEOUT_SECONDS)
        self.assertLessEqual(guard.SIBLING_SHORT_CHILD_TIMEOUT_SECONDS * 3, 30 * 60)

    def test_install_adds_default_timeouts_without_overriding_explicit_timeout(self) -> None:
        from scripts import canonical_v4_bundle

        module = guard.media_ffmpeg
        old_run = module._run
        old_check = module._check_output
        old_short_timeout = canonical_v4_bundle.SHORT_CHILD_TIMEOUT_SECONDS
        had_flag = hasattr(module, "_ISCO_RUN123_RUNTIME_LATENCY_GUARDED")
        old_flag = getattr(module, "_ISCO_RUN123_RUNTIME_LATENCY_GUARDED", None)
        fake_run = Mock(return_value="run-ok")
        fake_check = Mock(return_value="check-ok")
        try:
            module._run = fake_run
            module._check_output = fake_check
            if had_flag:
                delattr(module, "_ISCO_RUN123_RUNTIME_LATENCY_GUARDED")
            guard.install_run123_runtime_latency_guard()

            self.assertEqual(module._run(["ffmpeg"]), "run-ok")
            fake_run.assert_called_with(["ffmpeg"], timeout=guard.FFMPEG_COMMAND_TIMEOUT_SECONDS)

            self.assertEqual(module._check_output(["ffprobe"], timeout=11), "check-ok")
            fake_check.assert_called_with(["ffprobe"], timeout=11)
            self.assertEqual(
                canonical_v4_bundle.SHORT_CHILD_TIMEOUT_SECONDS,
                guard.SIBLING_SHORT_CHILD_TIMEOUT_SECONDS,
            )
        finally:
            module._run = old_run
            module._check_output = old_check
            canonical_v4_bundle.SHORT_CHILD_TIMEOUT_SECONDS = old_short_timeout
            if had_flag:
                module._ISCO_RUN123_RUNTIME_LATENCY_GUARDED = old_flag
            elif hasattr(module, "_ISCO_RUN123_RUNTIME_LATENCY_GUARDED"):
                delattr(module, "_ISCO_RUN123_RUNTIME_LATENCY_GUARDED")


if __name__ == "__main__":
    unittest.main()
