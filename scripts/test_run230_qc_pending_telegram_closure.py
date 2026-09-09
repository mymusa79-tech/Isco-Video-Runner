from __future__ import annotations

import tempfile
from pathlib import Path

from scripts import qc_pending_resume_bundle_v1 as bundle
from scripts import telegram_final_notify as notify
from scripts import telegram_gold_resume_control as gold
from scripts import telegram_v4_ingress as ingress


def _callbacks(keyboard: dict) -> list[str]:
    values: list[str] = []
    for row in keyboard.get("inline_keyboard") or []:
        for button in row or []:
            if isinstance(button, dict) and button.get("callback_data"):
                values.append(str(button["callback_data"]))
    return values


def test_opening_visual_audit_is_post_gold_optional() -> None:
    assert "opening-visual-audit.json" not in bundle.COMMON_REQUIRED_FILES
    assert "opening-visual-audit.json" in bundle.OPTIONAL_FILES


def test_diagnostics_locator_resolves_to_existing_v4_artifact_contract() -> None:
    assert (
        gold._resolved_artifact_name("isco-qc-pending-diagnostics-230")
        == "isco-resilient-v4-diagnostics-230"
    )
    try:
        gold._resolved_artifact_name("isco-qc-pending-diagnostics-not-a-run")
    except RuntimeError:
        pass
    else:
        raise AssertionError("malformed diagnostics locator must fail closed")


def test_terminal_card_exposes_gold_resume_only_for_qc_pending() -> None:
    assert notify.terminal_delivery_status(
        {"ISCO_QC_PENDING": "true", "JOB_STATUS": "failure", "CREATE_RELEASE_OUTCOME": "skipped"}
    ) == "qc_pending"
    keyboard = notify.terminal_keyboard(
        job_status="qc_pending",
        run_url="https://github.com/example/repo/actions/runs/1",
        run_id="1",
        progress_message_id="2",
        request_id="req-6312bcca7a93",
    )
    assert "cmd:goldresume-req-6312bcca7a93" in _callbacks(keyboard)

    failed = notify.terminal_keyboard(
        job_status="failure",
        run_url="https://github.com/example/repo/actions/runs/1",
        request_id="req-6312bcca7a93",
    )
    assert not any(value.startswith("cmd:goldresume-") for value in _callbacks(failed))


def test_generic_failure_promotes_exact_recovery_before_failed_state() -> None:
    original_recovery = ingress._current_qc_pending_recovery
    original_pending = ingress.qc_pending
    original_env = ingress._github_env
    original_load = ingress._load
    calls: list[tuple[str, object]] = []
    try:
        ingress._current_qc_pending_recovery = lambda: (Path("engine/output/x/qc-pending.json"), "isco-qc-pending-diagnostics-230")

        def fake_pending(**kwargs):
            calls.append(("qc_pending", kwargs))

        def fake_env(**kwargs):
            calls.append(("env", kwargs))

        def forbidden_load(path):
            raise AssertionError("generic failed-state path must not execute after exact QC_PENDING promotion")

        ingress.qc_pending = fake_pending
        ingress._github_env = fake_env
        ingress._load = forbidden_load
        with tempfile.TemporaryDirectory() as tmp:
            ingress.fail(
                state_path=Path(tmp) / "state.json",
                request_id="req-6312bcca7a93",
                request_sha256="a" * 64,
                authorization_id="b" * 32,
                reason="production_failed",
            )
        assert calls[0][0] == "qc_pending"
        assert calls[1][0] == "env"
        assert calls[1][1]["ISCO_QC_PENDING"] == "true"
    finally:
        ingress._current_qc_pending_recovery = original_recovery
        ingress.qc_pending = original_pending
        ingress._github_env = original_env
        ingress._load = original_load


def test_cancelled_failure_never_promotes_gold() -> None:
    original_recovery = ingress._current_qc_pending_recovery
    original_load = ingress._load
    original_mark = ingress.mark_dispatch_failed
    original_save = ingress._save
    calls: list[str] = []
    try:
        def forbidden_recovery():
            raise AssertionError("cancelled runs must not inspect Gold recovery")

        ingress._current_qc_pending_recovery = forbidden_recovery
        ingress._load = lambda path: {}
        ingress.mark_dispatch_failed = lambda *args, **kwargs: calls.append(str(kwargs.get("reason")))
        ingress._save = lambda *args, **kwargs: None
        with tempfile.TemporaryDirectory() as tmp:
            ingress.fail(
                state_path=Path(tmp) / "state.json",
                request_id="req-x",
                request_sha256="a" * 64,
                authorization_id="b" * 32,
                reason="production_cancelled",
            )
        assert calls == ["production_cancelled"]
    finally:
        ingress._current_qc_pending_recovery = original_recovery
        ingress._load = original_load
        ingress.mark_dispatch_failed = original_mark
        ingress._save = original_save


def main() -> int:
    tests = [
        test_opening_visual_audit_is_post_gold_optional,
        test_diagnostics_locator_resolves_to_existing_v4_artifact_contract,
        test_terminal_card_exposes_gold_resume_only_for_qc_pending,
        test_generic_failure_promotes_exact_recovery_before_failed_state,
        test_cancelled_failure_never_promotes_gold,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"Run #230 QC_PENDING Telegram closure PASS: {len(tests)} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
