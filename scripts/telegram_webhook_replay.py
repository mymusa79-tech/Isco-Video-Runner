from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from typing import Any

from scripts import telegram_control_active_ui as active
from scripts import telegram_control_panel as panel
from scripts import telegram_topic_memory_ui as memory_ui

# workflow_dispatch inputs have a finite payload budget; base64 expands bytes by ~4/3.
# Keeping raw Telegram updates below 48 KiB leaves safe room for the JSON envelope.
MAX_UPDATE_BYTES = 48 * 1024
SEEN_UPDATES_KEY = "telegram_webhook_seen_update_ids"
MAX_SEEN_UPDATES = 256


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


def replay_update(state_path: Path, update: dict[str, Any]) -> bool:
    """Run one authenticated Telegram update through the existing control plane exactly once.

    Telegram callback acknowledgement is intentionally suppressed here because the edge
    webhook answers it immediately. Authorization, run binding, selection, approval and
    production queue semantics remain inside the existing Python control plane.
    """
    update_id = int(update["update_id"])
    if _is_seen(state_path, update_id):
        print(f"Webhook update {update_id} already processed; no side effect")
        panel._github_output("needs_engine", "false")
        panel._github_output("needs_production", "false")
        return False

    memory_ui._install_policy()
    from scripts import telegram_persistent_control_ui as persistent_ui

    persistent_ui.install()

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

    sub.add_parser("webhook-active")

    args = parser.parse_args()
    if args.mode == "webhook-active":
        raise SystemExit(0 if webhook_active() else 1)

    update = decode_update(args.update_b64)
    replay_update(args.state, update)


if __name__ == "__main__":
    main()
