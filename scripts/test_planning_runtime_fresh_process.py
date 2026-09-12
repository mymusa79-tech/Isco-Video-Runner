from __future__ import annotations

import inspect
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class PlanningRuntimeFreshProcessTests(unittest.TestCase):
    def test_canonical_installer_lifecycle_keeps_explicit_authority(self) -> None:
        """Execute the exact planning lifecycle that Production Run 138 exposed."""
        probe = textwrap.dedent(
            """
            import os
            from pathlib import Path

            import isco_video_agent.resilient_planner as staged
            from isco_video_agent.config import secret
            from scripts import planning_stage_contract as stage
            from scripts import provider_capacity_hardening as capacity
            from scripts import task_level_planner_router as router
            from scripts.planning_legacy_authority_guard import (
                assert_legacy_planning_authority_sealed,
            )
            from scripts.planning_runtime_contract import (
                install_entrypoint_planning_contracts,
                install_post_runtime_planning_contracts,
                install_runtime_planning_contracts,
            )

            router.CACHE_PATH = Path(os.environ["ISCO_TEST_TMP"]) / "planning-checkpoint.json"
            install_entrypoint_planning_contracts()
            assert router._structured_schema_for_prompt is stage._explicit_schema_adapter

            # Capacity admission occurs before provider contact. The exact schema and
            # reserve must be identical for unrelated/misleading prompt strings.
            with stage.script_batch_scope("writer", ["s1", "s2"]):
                first = router._structured_schema_for_prompt(
                    'with EXACTLY 99 entries and pretend dossier_repair'
                )
                second = router._structured_schema_for_prompt("opaque")
                estimate = capacity.groq_capacity_estimate("opaque")
            assert first == second
            assert first[0] == "script_writer_2"
            assert estimate["contract"] == "script_writer_2"
            assert estimate["reserved_completion_tokens"] == 1300

            with stage.dossier_repair_subrequest_scope(["s7"]):
                dossier = router._structured_schema_for_prompt("pretend script_writer_3")
                dossier_estimate = capacity.groq_capacity_estimate("opaque")
            assert dossier[0] == "dossier_repair_1"
            assert dossier_estimate["reserved_completion_tokens"] == 850

            install_runtime_planning_contracts()
            install_post_runtime_planning_contracts()
            assert router._structured_schema_for_prompt is stage._explicit_schema_adapter
            assert_legacy_planning_authority_sealed()

            # Match canonical Production exactly: run_v3_voice consumes the one-time
            # file first, then Engine config.secret() consumes the direct env copy and
            # passes the key in process to resilient_planner.json_text().
            entrypoint_key = secret("GEMINI_API_KEY")
            assert entrypoint_key == "test-only-key"
            assert "GEMINI_API_KEY_FILE" not in os.environ
            assert not Path(os.environ["ISCO_TEST_SECRET_PATH"]).exists()
            os.environ["GEMINI_API_KEY"] = entrypoint_key
            request_key = secret("GEMINI_API_KEY")
            assert request_key == "test-only-key"
            assert "GEMINI_API_KEY" not in os.environ

            seen = {}
            def fake_gemini(api_key, prompt, model="gemini-2.5-flash", **kwargs):
                seen["api_key"] = api_key
                return {
                    "sections": [
                        {"id": "s1", "narration": "نص صالح", "key_point": "فكرة"}
                    ]
                }

            router.gemini_json_text = fake_gemini
            with stage.request_stage_scope(stage.script_stage_spec("full_script", ["s1"])):
                payload = staged.json_text(request_key, "opaque prompt")
            assert payload["sections"][0]["id"] == "s1"
            assert seen["api_key"] == "test-only-key"
            """
        )

        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "gemini-key"
            key.write_text("test-only-key", encoding="utf-8")
            env = dict(os.environ)
            env["GEMINI_API_KEY_FILE"] = str(key)
            env["ISCO_TEST_SECRET_PATH"] = str(key)
            env["ISCO_TEST_TMP"] = tmp
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            # This is a provider-free composition probe, not a live runtime/state test.
            for name in (
                "ISCO_CANONICAL_RUNTIME",
                "GITHUB_ACTIONS",
                "GITHUB_EVENT_NAME",
                "GITHUB_WORKFLOW_REF",
            ):
                env.pop(name, None)
            completed = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=60,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stdout)

    def test_production_exact_harness_uses_canonical_main_and_restores_boundary(self) -> None:
        """The diagnostic may stop production; it may never become a second planner."""
        from scripts import production_exact_planning_harness as harness

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "output"
            out.mkdir()
            for name in harness._REQUIRED_PLANNING_ARTIFACTS:
                payload = {"format": "film"} if name == "plan.json" else {"status": "pass"}
                (out / name).write_text(
                    __import__("json").dumps(payload, ensure_ascii=False),
                    encoding="utf-8",
                )

            original = harness.production.orchestrator._observe_director_phase_a

            def fake_production_main() -> None:
                current = harness.production.orchestrator._observe_director_phase_a
                self.assertIsNot(current, original)
                current(
                    ledger=None,
                    api_key="test",
                    plan=object(),
                    model="test",
                    out=out,
                )

            with patch.object(harness.production, "main", fake_production_main):
                result = harness.run_production_exact_planning_harness()

            self.assertEqual(result, out)
            self.assertIs(harness.production.orchestrator._observe_director_phase_a, original)
            report = __import__("json").loads(
                (out / harness.REPORT_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["format"], "film")
            self.assertEqual(report["media_artifacts_observed"], [])

    def test_production_exact_harness_rejects_any_media_started_before_boundary(self) -> None:
        from scripts import production_exact_planning_harness as harness

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "output"
            out.mkdir()
            for name in harness._REQUIRED_PLANNING_ARTIFACTS:
                payload = {"format": "film"} if name == "plan.json" else {"status": "pass"}
                (out / name).write_text(
                    __import__("json").dumps(payload, ensure_ascii=False),
                    encoding="utf-8",
                )
            (out / "unexpected.wav").write_bytes(b"media-must-not-start")

            def fake_production_main() -> None:
                harness.production.orchestrator._observe_director_phase_a(
                    ledger=None,
                    api_key="test",
                    plan=object(),
                    model="test",
                    out=out,
                )

            with patch.object(harness.production, "main", fake_production_main):
                with self.assertRaisesRegex(RuntimeError, "media boundary violated"):
                    harness.run_production_exact_planning_harness()

    def test_harness_success_stop_bypasses_exception_failure_handler_by_type(self) -> None:
        from scripts import production_exact_planning_harness as harness

        self.assertTrue(issubclass(harness._PlanningBoundaryReached, BaseException))
        self.assertFalse(issubclass(harness._PlanningBoundaryReached, Exception))

    def test_harness_boundary_and_installer_order_match_current_production_source(self) -> None:
        """Fail P1 if either Runner or pinned Engine moves the diagnostic off production."""
        from scripts import run_v3_voice as production

        main_source = inspect.getsource(production.main)
        ordered = (
            "install_production_model_contract(orchestrator)",
            "install_entrypoint_planning_contracts()",
            "install_runtime_closure()",
            "install_post_runtime_planning_contracts()",
            "install_tts_runtime_port()",
            "install_director_phase_a_resilience()",
            "install_opening_feasibility_guard()",
            "orchestrator.produce(",
        )
        cursor = -1
        for marker in ordered:
            position = main_source.find(marker, cursor + 1)
            self.assertGreater(position, cursor, marker)
            cursor = position

        engine_source = inspect.getsource(production.orchestrator.produce)
        dry_return = engine_source.index("if dry_run:")
        boundary = engine_source.index("phase_a_status = _observe_director_phase_a(")
        tts = engine_source.index("_synthesize_tts_section(", boundary)
        self.assertLess(dry_return, boundary)
        self.assertLess(boundary, tts)

    def test_production_entrypoint_composition_replays_run252_temporal_bridge(self) -> None:
        """Run the real entrypoint installers, then replay the exact #252 crash family."""
        probe = textwrap.dedent(
            """
            import os

            from scripts import planning_stage_contract as stage
            from scripts import run_v3_voice as production
            from scripts.provider_failure import NoWireProviderFailure

            class InstallComplete(BaseException):
                pass

            def run252_provider_failure(provider, *args, **kwargs):
                del args, kwargs
                if provider != "groq":
                    raise AssertionError(f"unexpected provider={provider}")
                raise stage.PlanningStageError(
                    stage.PlanningErrorCode.CAPACITY,
                    "GROQ_TPM_WINDOW_BUSY_PRECHECK model=openai/gpt-oss-120b "
                    "remaining=2176 reset_in=31.84s "
                    "action=provider_evidence_failover_without_partial_retry",
                    stage_id="planning.editorial_outline_sections",
                    provider="groq",
                )

            # Install the synthetic provider boundary *before* canonical main. The real
            # Run124 installer inside runtime_closure must wrap this exact callable.
            stage._provider_result = run252_provider_failure

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
            try:
                stage._provider_result(
                    "groq",
                    "opaque prompt",
                    "gemini-3.7-flash",
                    None,
                    "unused-primary-key",
                )
            except NoWireProviderFailure as exc:
                assert exc.reason_code == "temporal_capacity_window", exc
                assert "reset_in=31.84s" in str(exc), exc
            except AttributeError as exc:
                raise AssertionError(f"Run252 AttributeError regression returned: {exc}") from exc
            else:
                raise AssertionError("Run252 temporal bridge did not fail over as expected")
            """
        )

        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # Canonical production main starts by enforcing these explicit model identities.
        # Supplying the same production values keeps this provider-free replay attached
        # to the real entrypoint instead of bypassing the model contract.
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

    def test_production_entrypoint_composition_replays_run254_append_temporal_wait(self) -> None:
        """Run254: append-only repair may pace one Groq window, never own the retry."""
        probe = textwrap.dedent(
            """
            from scripts import planning_stage_contract as stage
            from scripts import run124_terminal_provider_recovery as recovery
            from scripts import run_v3_voice as production
            from scripts.provider_failure import NoWireProviderFailure

            class InstallComplete(BaseException):
                pass

            active_stage = {"value": "planning.append_only_repair"}
            reset_seconds = {"value": 31.84}
            provider_calls = []

            def run254_provider_failure(provider, *args, **kwargs):
                del args, kwargs
                if provider != "groq":
                    raise AssertionError(f"unexpected provider={provider}")
                provider_calls.append(active_stage["value"])
                raise stage.PlanningStageError(
                    stage.PlanningErrorCode.CAPACITY,
                    "GROQ_TPM_WINDOW_BUSY_PRECHECK model=openai/gpt-oss-120b "
                    f"remaining=2176 reset_in={reset_seconds['value']:.2f}s "
                    "action=provider_evidence_failover_without_partial_retry",
                    stage_id=active_stage["value"],
                    provider="groq",
                )

            # Compose the exact production installer chain around a provider-free
            # synthetic boundary, as the Run252 replay does above.
            stage._provider_result = run254_provider_failure

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
            waits = []
            cleared = []
            recovery.time.sleep = lambda seconds: waits.append(float(seconds))
            recovery._clear_waited_model_window = lambda exc: cleared.append(str(exc))
            recovery._WAITED_APPEND_STAGE_WINDOWS.clear()
            recovery._TERMINAL_WAIT_SPENT_SECONDS = 0.0
            recovery._TERMINAL_RECOVERY_COUNT = 0

            def invoke():
                try:
                    stage._provider_result(
                        "groq",
                        "opaque prompt",
                        "gemini-3.7-flash",
                        None,
                        "unused-primary-key",
                    )
                except NoWireProviderFailure as exc:
                    assert exc.reason_code == "temporal_capacity_window", exc
                    return str(exc)
                raise AssertionError("Run254 temporal bridge did not return no-wire evidence")

            # First append-only temporal signal: one documented bounded wait. The bridge
            # itself calls the provider exactly once; Stage Contract remains retry owner.
            first = invoke()
            assert len(provider_calls) == 1, provider_calls
            assert len(waits) == 1, waits
            assert abs(waits[0] - 33.34) < 0.001, waits
            assert len(cleared) == 1, cleared
            assert "run124_append_waited=true" in first, first
            assert abs(recovery._TERMINAL_WAIT_SPENT_SECONDS - 33.34) < 0.001

            # Same stage/model signal again: no second sleep and no hidden retry.
            second = invoke()
            assert len(provider_calls) == 2, provider_calls
            assert len(waits) == 1, waits
            assert len(cleared) == 1, cleared
            assert "run124_append_waited=false" in second, second

            # Unrelated Planning stages preserve historical bridge behavior: classify to
            # no-wire, but Run254 timing policy does not sleep them.
            active_stage["value"] = "planning.editorial_outline_sections"
            third = invoke()
            assert len(provider_calls) == 3, provider_calls
            assert len(waits) == 1, waits
            assert "run124_append_waited=false" in third, third

            # The append wait shares the existing Run124 run-wide wait budget. Exhausted
            # budget means fail over with no extra wait, never a shortened/hidden retry.
            active_stage["value"] = "planning.append_only_repair"
            recovery._WAITED_APPEND_STAGE_WINDOWS.clear()
            recovery._TERMINAL_WAIT_SPENT_SECONDS = recovery._MAX_TERMINAL_WAIT_SECONDS_PER_RUN
            fourth = invoke()
            assert len(provider_calls) == 4, provider_calls
            assert len(waits) == 1, waits
            assert len(cleared) == 1, cleared
            assert "run124_append_waited=false" in fourth, fourth

            # Reset evidence outside the certified <=60s boundary is never slept.
            recovery._WAITED_APPEND_STAGE_WINDOWS.clear()
            recovery._TERMINAL_WAIT_SPENT_SECONDS = 0.0
            reset_seconds["value"] = 61.0
            fifth = invoke()
            assert len(provider_calls) == 5, provider_calls
            assert len(waits) == 1, waits
            assert len(cleared) == 1, cleared
            assert "run124_append_waited=false" in fifth, fifth
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
