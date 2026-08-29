from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

# GitHub Actions invokes this file as ``python scripts/telegram_webhook_replay.py``.
# In that mode Python places ``scripts/`` rather than the repository root on
# sys.path, so absolute ``from scripts ...`` imports fail unless the root is
# restored explicitly. Keep this entrypoint robust for both direct and module use.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import telegram_control_active_ui as active
from scripts import telegram_control_panel as panel
from scripts import telegram_topic_memory_ui as memory_ui
from scripts.channel_os_telegram_adapter import callback_view, render_control_state
from scripts.telegram_release_approval import record_webhook_approval

# workflow_dispatch inputs have a finite payload budget; base64 expands bytes by ~4/3.
# Keeping raw Telegram updates below 48 KiB leaves safe room for the JSON envelope.
MAX_UPDATE_BYTES = 48 * 1024
SEEN_UPDATES_KEY = "telegram_webhook_seen_update_ids"
MAX_SEEN_UPDATES = 256
NOT_RELEASE_APPROVAL_EXIT = 3


def decode_update(encoded: str) -> dict[str, Any]:
    value = str(encoded or "").strip()
    if not value:
        raise RuntimeError("Webhook update payload is empty")
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise RuntimeError("Webhook update payload is not valid base64") from exc
    if not raw or len(raw) > MAX_UPDATE_BYTES:
        raise RuntimeError("Webhook update payload size is invalid")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("Webhook update payload is not valid UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Webhook update must be a JSON object")
    update_id = data.get("update_id")
    if not isinstance(update_id, int) or update_id < 0:
        raise RuntimeError("Webhook update_id is missing or invalid")
    if not isinstance(data.get("message") or data.get("callback_query"), dict):
        raise RuntimeError("Webhook update contains no supported message or callback_query")
    return data


def _seen_ids(state: dict[str, Any]) -> list[int]:
    raw = state.get(SEEN_UPDATES_KEY)
    if not isinstance(raw, list):
        return []
    result: list[int] = []
    for item in raw:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value >= 0 and value not in result:
            result.append(value)
    return result[-MAX_SEEN_UPDATES:]


def _mark_seen(state_path: Path, update_id: int) -> None:
    state = panel.load_state(state_path)
    seen = _seen_ids(state)
    if update_id not in seen:
        seen.append(update_id)
    state[SEEN_UPDATES_KEY] = seen[-MAX_SEEN_UPDATES:]
    state["last_event_at"] = panel._now()
    panel.save_state(state_path, state)


def _is_seen(state_path: Path, update_id: int) -> bool:
    return update_id in _seen_ids(panel.load_state(state_path))


def _record_release_approval_if_present(state_path: Path, update: dict[str, Any]) -> bool:
    state = panel.load_state(state_path)
    bound = record_webhook_approval(
        state,
        update=update,
        allowed_user_id=panel._read_secret_file("TELEGRAM_ALLOWED_USER_ID_FILE"),
        allowed_chat_id=panel._read_secret_file("TELEGRAM_CHAT_ID_FILE"),
    )
    if bound is None:
        return False
    panel.save_state(state_path, state)
    panel._github_output("needs_engine", "false")
    panel._github_output("needs_production", "false")
    print(
        "Release approval receipt persisted through webhook ingress: "
        f"approval={bound.approval_id} decision={bound.decision.value}"
    )
    return True


def replay_release_approval_only(state_path: Path, update: dict[str, Any]) -> bool:
    """Consume only an L6 release approval while general control is read-only.

    Production may be active when the release-candidate approval button is pressed.
    This narrow path never installs or invokes the legacy/stateful command parser. It
    therefore cannot execute research, mutate topic memory, or dispatch production.
    Duplicate webhook update ids are harmless and treated as already consumed.
    """
    update_id = int(update["update_id"])
    if _is_seen(state_path, update_id):
        print(f"Webhook update {update_id} already processed; no side effect")
        panel._github_output("needs_engine", "false")
        panel._github_output("needs_production", "false")
        return True
    if not _record_release_approval_if_present(state_path, update):
        return False
    _mark_seen(state_path, update_id)
    return True


def _authorized_channel_os_callback(update: dict[str, Any]) -> tuple[str, int | str] | None:
    callback = update.get("callback_query")
    if not isinstance(callback, dict):
        return None
    view = callback_view(callback.get("data"))
    if view is None:
        return None
    allowed_text = panel._read_secret_file("TELEGRAM_ALLOWED_USER_ID_FILE", required=True)
    allowed_chat = panel._read_secret_file("TELEGRAM_CHAT_ID_FILE", required=True)
    try:
        allowed_user = int(allowed_text)
    except ValueError as exc:
        raise RuntimeError("TELEGRAM_ALLOWED_USER_ID_FILE is invalid") from exc
    authorized, chat_id, _ = panel._authorized_user(update, allowed_user, allowed_chat)
    if not authorized or chat_id is None:
        raise RuntimeError("Channel OS callback is not authorized for this user/chat")
    return view, chat_id


def _replay_channel_os_if_present(state_path: Path, update: dict[str, Any]) -> bool:
    """Render Channel OS through the existing L6 webhook transport boundary.

    The Channel OS adapter itself owns no Telegram credential, polling, send method,
    retry policy, production dispatch, or publication authority. This bridge is a
    read-only query response. Critical Telegram side effects continue to use the L6
    durable outbox; a Mission Control refresh has no authoritative state mutation.
    """
    resolved = _authorized_channel_os_callback(update)
    if resolved is None:
        return False
    view, chat_id = resolved
    state = panel.load_state(state_path)
    repository = str(os.environ.get("GITHUB_REPOSITORY") or "mymusa79-tech/Isco-Video-Runner").strip()
    github_token = str(os.environ.get("GITHUB_TOKEN") or "").strip()
    memory_root = Path(os.environ.get("RUNNER_TEMP") or state_path.parent) / "channel-os-mission-control"
    text, keyboard = render_control_state(
        state,
        repository=repository,
        github_token=github_token,
        memory_root=memory_root,
        view=view,
    )
    token = panel._read_secret_file("TELEGRAM_BOT_TOKEN_FILE", required=True)
    panel.TelegramClient(token).send(chat_id, text, keyboard=keyboard)
    panel._github_output("needs_engine", "false")
    panel._github_output("needs_production", "false")
    print(f"Channel OS Mission Control rendered through webhook ingress: view={view}")
    return True


def replay_update(state_path: Path, update: dict[str, Any]) -> bool:
    """Run one authenticated Telegram update through the existing control plane exactly once.

    Release approval callbacks are consumed directly by the webhook-owned L6 adapter.
    Channel OS callbacks are read-only Mission Control projections rendered at this
    same ingress boundary. All other stateful callbacks continue through the certified
    legacy parser using an injected already-received update; that injection is not a
    live Telegram poll.
    """
    update_id = int(update["update_id"])
    if _is_seen(state_path, update_id):
        print(f"Webhook update {update_id} already processed; no side effect")
        panel._github_output("needs_engine", "false")
        panel._github_output("needs_production", "false")
        return False

    if replay_release_approval_only(state_path, update):
        return True

    if _replay_channel_os_if_present(state_path, update):
        _mark_seen(state_path, update_id)
        return True

    # Install the exact same UI stack as telegram_topic_memory_ui.install().
    # Edge renders the long/short split locally, while saved-topic selection is
    # deliberately stateful and is replayed here. Omitting _install_library_split()
    # made the visible Edge callbacks and the Python callback contract diverge.
    memory_ui._install_policy()
    from scripts import telegram_persistent_control_ui as persistent_ui

    persistent_ui.install()
    memory_ui._install_library_split()
    memory_ui._install_choice_clarity()

    original_call = panel.TelegramClient.call
    original_answer = panel.TelegramClient.answer_callback

    def replay_call(self, method: str, payload: dict[str, Any] | None = None):
        if method == "getUpdates":
            return [update]
        return original_call(self, method, payload)

    def replay_answer(self, callback_id: str, text: str = "") -> None:
        # The Cloudflare edge already answered the callback so the Telegram spinner
        # stops immediately. A second answer can become invalid by the time Actions starts.
        return None

    panel.TelegramClient.call = replay_call
    panel.TelegramClient.answer_callback = replay_answer
    try:
        active._poll(state_path)
    finally:
        panel.TelegramClient.call = original_call
        panel.TelegramClient.answer_callback = original_answer

    _mark_seen(state_path, update_id)
    print(f"Webhook update {update_id} processed and marked idempotent")
    return True


def webhook_active() -> bool:
    token = panel._read_secret_file("TELEGRAM_BOT_TOKEN_FILE")
    if not token:
        return False
    info = panel.TelegramClient(token).call("getWebhookInfo", {})
    return isinstance(info, dict) and bool(str(info.get("url") or "").strip())


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram edge-webhook bridge for Isco control plane")
    sub = parser.add_subparsers(dest="mode", required=True)

    replay = sub.add_parser("replay")
    replay.add_argument("--state", required=True, type=Path)
    replay.add_argument("--update-b64", required=True)

    approval = sub.add_parser("release-approval-only")
    approval.add_argument("--state", required=True, type=Path)
    approval.add_argument("--update-b64", required=True)

    sub.add_parser("webhook-active")

    args = parser.parse_args()
    if args.mode == "webhook-active":
        raise SystemExit(0 if webhook_active() else 1)

    update = decode_update(args.update_b64)
    if args.mode == "release-approval-only":
        consumed = replay_release_approval_only(args.state, update)
        raise SystemExit(0 if consumed else NOT_RELEASE_APPROVAL_EXIT)
    replay_update(args.state, update)


if __name__ == "__main__":
    main()
