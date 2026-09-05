from __future__ import annotations

import os
import sys
from pathlib import Path

# GitHub Actions executes this wrapper as ``python scripts/telegram_webhook_replay.py``.
# Direct script execution puts ``scripts/`` rather than the repository root on
# sys.path. Restore the root before importing the package-owned replay core.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import telegram_webhook_replay_core as core


def __getattr__(name: str):
    return getattr(core, name)


def _install_v5_after_active() -> None:
    if getattr(core.active, "_isco_v5_replay_hooked", False):
        return
    original_install = core.active._install

    def install_with_v5() -> None:
        original_install()
        from scripts import telegram_creator_control_center_v5 as creator_v5
        from scripts import telegram_long_format_policy as long_format_policy
        from scripts import telegram_operator_mission_control as operator_mission_control
        from scripts import telegram_session_continuity as session_continuity

        creator_v5.install()
        # Webhook replay is the live path for Telegram button presses. Install
        # continuity only after active UI + V5 have finalized the approval stack.
        session_continuity.install(active=core.active, panel=core.panel)
        # Final operator projection: state wording and confirmation receipts only.
        # Production authority remains the existing exact typed-confirmation seam.
        operator_mission_control.install()
        # Run #203 closure: webhook replay must bind the same Long outer-format
        # policy as the certified fallback-polling entrypoint. Install it last over
        # the final approval owner so a live webhook approval cannot bypass
        # ``long -> auto -> film/story`` or the canonical research-pack contract.
        long_format_policy.install(panel=core.panel)

    core.active._install = install_with_v5
    core.active._isco_v5_replay_hooked = True


def _control_state_path() -> Path | None:
    raw = str(os.environ.get("CONTROL_STATE_PATH") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_file() else None


def _state_arg_from_argv() -> Path | None:
    args = sys.argv[1:]
    try:
        index = args.index("--state")
    except ValueError:
        return None
    if index + 1 >= len(args):
        return None
    path = Path(args[index + 1])
    return path if path.is_file() else None


def _repair_current_long_target_if_available(path: Path | None = None) -> bool:
    """Repair only the currently approved legacy Long target, never historical state."""
    state_path = path or _control_state_path()
    if state_path is None or not state_path.is_file():
        return False
    from scripts import telegram_long_format_policy as long_format_policy

    state = core.panel.load_state(state_path)
    changed = long_format_policy.migrate_current_production_target(state, panel=core.panel)
    if not changed:
        return False
    core.panel.save_state(state_path, state)
    target = state.get("production_target") or {}
    print(
        "Run203 Long target migration applied before Telegram control: "
        f"request={target.get('request_id')} sha={target.get('request_sha256')}"
    )
    return True


def _reconcile_used_history_if_available() -> dict[str, int] | None:
    """Repair historical Used projection from completed durable Telegram receipts."""
    path = _control_state_path()
    if path is None:
        return None
    from scripts.telegram_used_history_reconcile import reconcile_file

    result = reconcile_file(path)
    if result["added"]:
        print(
            "Telegram Used history backfill applied from completed receipts: "
            f"added={result['added']} processed={result['processed']}"
        )
    return result


def _durable_pending_research_exists() -> bool:
    """Return whether the restored control state still owns pending research work.

    With Edge webhook ingress enabled, scheduled Actions must never fall back to
    Telegram getUpdates polling. They still need to service durable research work
    that a prior webhook run queued but could not finish because a live provider
    timed out. The workflow uses ``webhook-active`` as its ingress-suppression gate,
    so this wrapper deliberately returns a non-zero gate result only for that
    pending-work case; the poll entrypoint then claims the pending work without
    polling Telegram.
    """
    path = _control_state_path()
    if path is None:
        return False
    try:
        state = core.panel.load_state(path)
    except Exception:
        return False
    actions = state.get("pending_actions")
    if not isinstance(actions, list):
        return False
    return any(
        isinstance(item, dict)
        and str(item.get("status") or "") == "pending"
        and str(item.get("kind") or "long") in {"long", "short"}
        for item in actions
    )


def replay_update(state_path, update):
    _install_v5_after_active()
    from scripts import telegram_creator_control_center_v5 as creator_v5

    # Direct adapter callers use this wrapper rather than core.main(). Keep the same
    # pre-replay state migration that the CLI path performs below.
    durable_path = Path(state_path)
    if durable_path.is_file():
        _repair_current_long_target_if_available(durable_path)
        from scripts.telegram_used_history_reconcile import reconcile_file

        reconcile_file(durable_path)

    # The replay core substitutes getUpdates with the already-authorized webhook
    # update. Preserve that callback's message identity as a class-level fallback
    # so V5 can still edit the exact Telegram card in place after the replay core
    # replaces TelegramClient.call for this one invocation.
    context = creator_v5._context_from_update(update)
    core.panel.TelegramClient._isco_v5_surface_context = context
    try:
        return core.replay_update(state_path, update)
    finally:
        core.panel.TelegramClient._isco_v5_surface_context = None


def main() -> None:
    _install_v5_after_active()
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "webhook-active":
        # This command is executed by every scheduled control pass after encrypted
        # state restore. Reconcile first; the workflow's existing persistence step
        # will write any idempotent backfill/migration to control-plane-state.
        _reconcile_used_history_if_available()
        _repair_current_long_target_if_available()
        active_now = core.webhook_active()
        if active_now and _durable_pending_research_exists():
            print(
                "Telegram webhook is active, but durable pending research requires "
                "a scheduler service pass; live Telegram polling remains suppressed"
            )
            raise SystemExit(1)
        raise SystemExit(0 if active_now else 1)
    if mode == "replay":
        # core.main() owns CLI parsing and replay execution, but this wrapper owns
        # the policy migration. Repair the exact restored state before core loads it.
        _repair_current_long_target_if_available(_state_arg_from_argv())
    core.main()


if __name__ == "__main__":
    main()
