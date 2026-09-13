from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _probe_env() -> dict[str, str]:
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
    return env


class TemporalCapacityFinalCompositionTests(unittest.TestCase):
    def _run_probe(self, probe: str) -> None:
        completed = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(probe)],
            cwd=ROOT,
            env=_probe_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=90,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)

    def test_final_production_composition_preserves_run258_prewire_append_pacing(self) -> None:
        """Replay Run #258 through the final, real production wrapper chain.

        The test creates provider capacity evidence at Run123's actual local preflight,
        not at Stage Contract's _provider_result seam. A fake HTTP response is available
        only after Run124's bounded wait clears the model window. This proves Run125
        preserves no-wire provenance, each bound append request may wait once, the
        three-call append topology remains finite, and telemetry counts only real HTTP.
        """
        self._run_probe(
            """
            import json
            from dataclasses import replace

            from scripts import planning_stage_contract as stage
            from scripts import provider_capacity_hardening as capacity
            from scripts import run124_terminal_provider_recovery as recovery
            from scripts import run125_cache_prefix_contract as cache_contract
            from scripts import run125_capacity_routing_closure as closure
            from scripts import run_v3_voice as production
            from scripts import task_level_planner_router as router

            class InstallComplete(BaseException):
                pass

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
            post_calls = []
            clock = {"monotonic": 1000.0, "epoch": 2000.0}

            def fake_sleep(seconds):
                seconds = float(seconds)
                waits.append(seconds)
                clock["monotonic"] += seconds
                clock["epoch"] += seconds

            recovery.time.sleep = fake_sleep
            recovery.time.monotonic = lambda: clock["monotonic"]
            recovery.time.time = lambda: clock["epoch"]
            capacity._persist_model_states = lambda: None
            router._read_secret_file = lambda _name: "test-groq-key"

            class FakeResponse:
                ok = True
                status_code = 200
                headers = {}
                text = ""

                @staticmethod
                def json():
                    return {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "additions": [
                                                {
                                                    "id": "s1",
                                                    "append_text": (
                                                        "إضافة صالحة تحافظ على نفس الفكرة"
                                                    ),
                                                }
                                            ]
                                        },
                                        ensure_ascii=False,
                                    )
                                },
                            }
                        ]
                    }

            def fake_post(url, **kwargs):
                post_calls.append(
                    {
                        "url": url,
                        "model": kwargs["json"]["model"],
                    }
                )
                return FakeResponse()

            router.requests.post = fake_post
            recovery._WAITED_APPEND_STAGE_WINDOWS.clear()
            recovery._TERMINAL_WAIT_SPENT_SECONDS = 0.0
            recovery._TERMINAL_RECOVERY_COUNT = 0
            router._TELEMETRY.clear()
            capacity._GROQ_MODEL_RATE_STATE.clear()
            closure._ACTIVE_GROQ_INDEX = 0

            models = tuple(cache_contract._PRODUCTION_GROQ_MODEL_POOL)
            assert models == ("openai/gpt-oss-20b", "openai/gpt-oss-120b")

            def arm_window(model, reset_seconds):
                state = capacity._GROQ_MODEL_RATE_STATE.setdefault(
                    model, capacity._empty_model_state()
                )
                state.update(
                    {
                        "actual_tpm_limit": 100000,
                        "remaining_tokens": 0,
                        "reset_at_epoch": clock["epoch"] + float(reset_seconds),
                        "contacted": True,
                        "blocked_reason": None,
                    }
                )

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

            reset_windows = (29.97, 4.0, 2.0)
            for index, reset_seconds in enumerate(reset_windows):
                if index == 0:
                    for model in models:
                        arm_window(model, reset_seconds)
                else:
                    arm_window(models[-1], reset_seconds)
                with stage.request_stage_scope(spec):
                    result = stage.staged.json_text(
                        "unused-primary-key",
                        f"opaque append logical request {index}",
                        model="gemini-3.7-flash",
                    )
                assert result["additions"][0]["id"] == "s1"

            assert len(post_calls) == 3, post_calls
            assert [round(item, 2) for item in waits] == [31.47, 5.5, 3.5], waits
            assert len(recovery._WAITED_APPEND_STAGE_WINDOWS) == 3
            assert abs(recovery._TERMINAL_WAIT_SPENT_SECONDS - 40.47) < 0.001

            # append_retry_guard has no fourth logical call. Even with trustworthy reset
            # evidence, Run124 must now fail closed without sleeping or touching HTTP.
            arm_window(models[-1], 1.0)
            before_waits = list(waits)
            before_posts = list(post_calls)
            try:
                with stage.request_stage_scope(spec):
                    stage.staged.json_text(
                        "unused-primary-key",
                        "opaque impossible fourth append logical request",
                        model="gemini-3.7-flash",
                    )
            except stage.PlanningStageError as exc:
                assert "GROQ_TPM_WINDOW_BUSY_PRECHECK" in str(exc), exc
            else:
                raise AssertionError("fourth append temporal wait unexpectedly succeeded")
            assert waits == before_waits, waits
            assert post_calls == before_posts, post_calls

            groq_events = [
                item for item in router.get_telemetry()
                if item.get("provider") == "groq"
            ]
            wire_events = [
                item for item in groq_events if item.get("wire_attempted") is True
            ]
            no_wire_events = [
                item for item in groq_events if item.get("wire_attempted") is False
            ]
            assert len(wire_events) == 3, groq_events
            assert all(item.get("result") == "success" for item in wire_events), wire_events
            assert all(item.get("provider_attempt") == 1 for item in wire_events), wire_events
            assert len(no_wire_events) == 1, groq_events
            assert no_wire_events[0].get("provider_attempt") is None, no_wire_events
            """
        )

    def test_final_composition_retains_long_429_cooldown_metadata(self) -> None:
        """Run125 normalization must not erase the deadline Stage Contract persists."""
        self._run_probe(
            """
            import tempfile
            from dataclasses import replace
            from pathlib import Path

            from scripts import planning_stage_contract as stage
            from scripts import run_v3_voice as production
            from scripts import task_level_planner_router as router

            class InstallComplete(BaseException):
                pass

            temp_dir = tempfile.TemporaryDirectory()
            router.CACHE_PATH = Path(temp_dir.name) / "planning-checkpoint.json"

            def stop_after_all_runtime_installers():
                raise InstallComplete()

            production.start_progress = stop_after_all_runtime_installers
            try:
                production.main()
            except InstallComplete:
                pass
            else:
                raise AssertionError("canonical production main did not reach installer boundary")

            valid = {
                "sections": [
                    {
                        "id": "s1",
                        "narration": "نص صالح",
                        "key_point": "key-s1",
                    }
                ]
            }
            calls = {"gemini": 0, "groq": 0}

            def gemini_short_window(*_args, **_kwargs):
                calls["gemini"] += 1
                router._last_call_rate_limit_headers["retry_after"] = "60"
                raise RuntimeError(
                    "GEMINI_HTTP_429 status=429 quota exceeded requests per minute"
                )

            def groq_success(_prompt):
                calls["groq"] += 1
                return valid

            router.gemini_json_text = gemini_short_window
            router._groq_call = groq_success
            router._TELEMETRY.clear()

            base_spec = stage.script_stage_spec("full_script", ["s1"])
            spec = replace(
                base_spec,
                provider_policy=replace(
                    base_spec.provider_policy,
                    providers=("gemini", "groq"),
                    max_attempts_per_provider=1,
                    max_total_attempts=2,
                ),
            )

            with stage.request_stage_scope(spec):
                first = stage.staged.json_text(
                    "request-key", "run-258 cooldown stage one"
                )
            with stage.request_stage_scope(spec):
                second = stage.staged.json_text(
                    "request-key", "run-258 cooldown stage two"
                )

            assert first == valid and second == valid
            assert calls == {"gemini": 1, "groq": 2}, calls
            gemini_events = [
                item for item in router.get_telemetry()
                if item.get("provider") == "gemini"
            ]
            assert [item.get("result") for item in gemini_events] == [
                "retry_after_exceeds_budget",
                "transient-cooldown",
            ], gemini_events
            first_event, skipped_event = gemini_events
            assert first_event.get("http_status") == 429, first_event
            assert first_event.get("quota_scope") == "short_window", first_event
            assert float(first_event.get("retry_after")) == 60.0, first_event
            assert first_event.get("wire_attempted") is True, first_event
            assert skipped_event.get("wire_attempted") is False, skipped_event
            assert skipped_event.get("provider_attempt") is None, skipped_event
            """
        )

    def test_final_production_composition_never_waits_for_fixed_groq_capacity(self) -> None:
        """Fixed request-vs-limit capacity must stay fail-fast after final composition.

        This runs in its own fresh process because the previous temporal test intentionally
        leaves the provider in a bounded cooldown after the repeated-busy proof. Production
        requests likewise do not erase that state merely to make a diagnostic scenario run.
        """
        self._run_probe(
            """
            from dataclasses import replace

            from scripts import planning_stage_contract as stage
            from scripts import run124_terminal_provider_recovery as recovery
            from scripts import run_v3_voice as production
            from scripts import task_level_planner_router as router

            class InstallComplete(BaseException):
                pass

            provider_calls = []

            def fixed_capacity(provider, *args, **kwargs):
                del args, kwargs
                if provider != "groq":
                    raise AssertionError(f"unexpected provider={provider}")
                provider_calls.append(provider)
                raise router.NoWireProviderFailure(
                    "GROQ_ACTUAL_TPM_BELOW_REQUEST",
                    "model=openai/gpt-oss-120b required=9000 actual_limit=8000",
                )

            stage._provider_result = fixed_capacity

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
            recovery.time.sleep = lambda seconds: waits.append(float(seconds))
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

            assert waits == [], waits
            assert provider_calls == ["groq"], provider_calls
            assert recovery._TERMINAL_WAIT_SPENT_SECONDS == 0.0
            """
        )


if __name__ == "__main__":
    unittest.main()
