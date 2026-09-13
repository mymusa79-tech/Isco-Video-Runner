from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TemporalCapacityFinalCompositionTests(unittest.TestCase):
    def test_final_production_composition_preserves_run255_prewire_append_pacing(self) -> None:
        """Protect the live Run255 shape after every canonical Production installer.

        This is intentionally a fresh-process composition test.  It starts at the same
        raw pre-HTTP NoWireProviderFailure emitted by Run123 Groq pacing, lets canonical
        production install every planning/runtime patch, and then exercises the actual
        Planning Stage Contract.  A future installer may not silently remove, duplicate,
        or turn the pre-wire pacing continuation into a counted provider retry.
        """
        probe = textwrap.dedent(
            """
            from dataclasses import replace

            from scripts import planning_stage_contract as stage
            from scripts import run124_terminal_provider_recovery as recovery
            from scripts import run_v3_voice as production
            from scripts import task_level_planner_router as router

            class InstallComplete(BaseException):
                pass

            state = {"mode": "recover", "recover_calls": 0}
            provider_calls = []

            def temporal_busy():
                raise router.NoWireProviderFailure(
                    "GROQ_TPM_WINDOW_BUSY_PRECHECK",
                    "model=openai/gpt-oss-120b required_estimate=4325 "
                    "remaining=4003 reset_in=29.97s action=failover_without_http",
                )

            def live_provider_result(provider, *args, **kwargs):
                del args, kwargs
                if provider != "groq":
                    raise AssertionError(f"unexpected provider={provider}")
                provider_calls.append(state["mode"])
                if state["mode"] == "recover":
                    state["recover_calls"] += 1
                    if state["recover_calls"] == 1:
                        temporal_busy()
                    return {
                        "additions": [
                            {
                                "id": "s1",
                                "append_text": "إضافة صالحة تحافظ على نفس الفكرة",
                            }
                        ]
                    }
                if state["mode"] == "always_busy":
                    temporal_busy()
                if state["mode"] == "impossible":
                    raise router.NoWireProviderFailure(
                        "GROQ_ACTUAL_TPM_BELOW_REQUEST",
                        "model=openai/gpt-oss-120b required=9000 actual_limit=8000",
                    )
                raise AssertionError(f"unknown mode={state['mode']}")

            # Install the provider-free production boundary before canonical main.  The
            # real Run124 installer must wrap this callable and that wrapper must survive
            # all later runtime/post-runtime installers.
            stage._provider_result = live_provider_result

            def stop_after_all_runtime_installers():
                raise InstallComplete()

            production.start_progress = stop_after_all_runtime_installers
            try:
                production.main()
            except InstallComplete:
                pass
            else:
                raise AssertionError("canonical production main did not reach installer boundary")

            stage.assert_planning_stage_contract_installed()
            assert getattr(
                stage._provider_result,
                recovery._STAGE_PROVIDER_BRIDGE_MARKER,
                False,
            ), "final production composition lost Run124 temporal provider bridge"

            waits = []
            cleared = []
            clock = {"now": 1000.0}

            def fake_sleep(seconds):
                seconds = float(seconds)
                waits.append(seconds)
                clock["now"] += seconds

            def fake_monotonic():
                return clock["now"]

            # recovery.time and stage.time are the same Python time module.  Advance a
            # deterministic clock whenever the Run255 wait occurs so Stage Contract's
            # independent 1.5s provider-spacing guard sees real elapsed time instead of
            # inventing a second sleep only because this test mocked sleep to return
            # instantly.  The waits list therefore measures actual logical waits.
            recovery.time.sleep = fake_sleep
            recovery.time.monotonic = fake_monotonic
            recovery._clear_waited_model_window = lambda exc: cleared.append(str(exc))
            recovery._WAITED_APPEND_STAGE_WINDOWS.clear()
            recovery._TERMINAL_WAIT_SPENT_SECONDS = 0.0
            recovery._TERMINAL_RECOVERY_COUNT = 0
            router._TELEMETRY.clear()

            base_spec = stage.append_stage_spec(["s1"])
            spec = replace(
                base_spec,
                provider_policy=replace(
                    base_spec.provider_policy,
                    providers=("groq",),
                    max_attempts_per_provider=2,
                    max_total_attempts=2,
                ),
            )

            # Real Run255 shape: the first call is pre-wire busy, so pacing may wait once
            # and continue the SAME logical attempt.  Stage Contract must record only the
            # eventual HTTP success as provider_attempt=1.
            with stage.request_stage_scope(spec):
                result = stage.staged.json_text(
                    "unused-primary-key",
                    "opaque append prompt",
                    model="gemini-3.7-flash",
                )
            assert result["additions"][0]["id"] == "s1"
            assert provider_calls == ["recover", "recover"], provider_calls
            assert len(waits) == 1 and abs(waits[0] - 31.47) < 0.001, waits
            assert len(cleared) == 1, cleared
            assert abs(recovery._TERMINAL_WAIT_SPENT_SECONDS - 31.47) < 0.001

            groq_events = [
                item for item in router.get_telemetry()
                if item.get("provider") == "groq"
            ]
            assert len(groq_events) == 1, groq_events
            assert groq_events[0].get("result") == "success", groq_events
            assert groq_events[0].get("provider_attempt") == 1, groq_events
            assert groq_events[0].get("wire_attempted") is True, groq_events

            # Same stage/model temporal signal again must never form a hidden wait loop.
            # The once-per-stage/model guard has already been spent, so the raw no-wire
            # evidence bubbles to the Stage Contract with no second sleep or HTTP count.
            state["mode"] = "always_busy"
            before_waits = list(waits)
            before_calls = len(provider_calls)
            try:
                with stage.request_stage_scope(spec):
                    stage.staged.json_text(
                        "unused-primary-key",
                        "opaque append prompt second",
                        model="gemini-3.7-flash",
                    )
            except stage.PlanningStageError as exc:
                assert "GROQ_TPM_WINDOW_BUSY_PRECHECK" in str(exc), exc
            else:
                raise AssertionError("repeated Run255 temporal signal unexpectedly succeeded")
            assert waits == before_waits, waits
            assert len(provider_calls) == before_calls + 1, provider_calls

            # Fixed/impossible request capacity is not a temporal window and must never
            # be slept simply because it is also pre-wire Groq evidence.
            state["mode"] = "impossible"
            before_waits = list(waits)
            before_calls = len(provider_calls)
            try:
                with stage.request_stage_scope(spec):
                    stage.staged.json_text(
                        "unused-primary-key",
                        "opaque impossible append prompt",
                        model="gemini-3.7-flash",
                    )
            except stage.PlanningStageError as exc:
                assert "GROQ_ACTUAL_TPM_BELOW_REQUEST" in str(exc), exc
            else:
                raise AssertionError("fixed Groq capacity failure unexpectedly succeeded")
            assert waits == before_waits, waits
            assert len(provider_calls) == before_calls + 1, provider_calls
            """
        )

        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["GEMINI_CONTENT_MODEL"] = "gemini-3.7-flash"
        env["GEMINI_TTS_MODEL"] = "gemini-3.1-flash-tts-preview"
        for name in (
            "ISCO_CANONICAL_RUNTIME",
            "GITHUB_ACTIONS",
            "GITHUB_EVENT_NAME",
            "GITHUB_WORKFLOW_REF",
            "REQUEST_FILE",
            "GEMINI_API_KEY",
            "GEMINI_API_KEY_FILE",
            "PEXELS_API_KEY",
            "PEXELS_API_KEY_FILE",
        ):
            env.pop(name, None)

        completed = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=90,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)


if __name__ == "__main__":
    unittest.main()
