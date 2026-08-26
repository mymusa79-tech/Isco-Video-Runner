from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

import isco_video_agent.orchestrator as orchestrator

from scripts import telegram_operations_ui as ops_ui

# Stage order follows the real pipeline: planning, TTS, visual clip prep, final mux.
# Best-effort only - a Telegram outage must never fail real production, so every
# function here swallows its own notification exceptions.
_STAGES = list(ops_ui.STAGE_LABELS.items())

_state = {
    "token": "",
    "chat_id": "",
    "message_id": None,
    "completed": set(),
    "current_stage": None,
    "run_id": "",
    "run_number": "",
    "topic": "",
}


def _read_secret_file_optional(env_name: str) -> str:
    """Optional Telegram secret reader: absence disables progress without failing production."""
    path = os.environ.get(env_name, "").strip()
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _read_request_topic_optional() -> str:
    path = os.environ.get("REQUEST_FILE", "").strip()
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("topic") or "").strip()


def _telegram_request(method: str, payload: dict) -> dict | None:
    """Log each Telegram call while keeping progress best-effort and token-safe."""
    if not _state["token"]:
        return None
    url = f"https://api.telegram.org/bot{_state['token']}/{method}"
    data = urllib.parse.urlencode(payload).encode("utf-8")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"Telegram {method} failed: {type(exc).__name__}: {exc}")
        return None
    if not result.get("ok"):
        print(f"Telegram {method} failed: {result.get('description', 'unknown API error')}")
        return None
    print(f"Telegram {method} succeeded")
    return result


def _render() -> str:
    return ops_ui.render_progress_text(
        run_number=str(_state.get("run_number") or ""),
        topic=str(_state.get("topic") or ""),
        current_stage=_state.get("current_stage"),
        completed=_state["completed"],
    )


def start_progress() -> None:
    """Create the single lifecycle message later edited by stages and terminal notify."""
    token = _read_secret_file_optional("TELEGRAM_BOT_TOKEN_FILE")
    chat_id = _read_secret_file_optional("TELEGRAM_CHAT_ID_FILE")
    _state["token"] = token
    _state["chat_id"] = chat_id
    _state["completed"] = set()
    _state["current_stage"] = None
    _state["message_id"] = None
    _state["run_id"] = os.environ.get("GITHUB_RUN_ID", "").strip()
    _state["run_number"] = os.environ.get("GITHUB_RUN_NUMBER", "").strip()
    _state["topic"] = _read_request_topic_optional()
    if not token or not chat_id:
        print("Telegram progress tracking disabled: bot token or chat id not configured")
        return
    print("Telegram notify: sendMessage (initial lifecycle message)")
    resp = _telegram_request("sendMessage", {"chat_id": chat_id, "text": _render()})
    if not resp:
        return
    message_id = resp["result"]["message_id"]
    _state["message_id"] = message_id
    print(f"Telegram progress message created: message_id={message_id}")
    runner_temp = os.environ.get("RUNNER_TEMP", "")
    if runner_temp:
        try:
            with open(os.path.join(runner_temp, "telegram-progress-message-id.txt"), "w", encoding="utf-8") as f:
                f.write(str(message_id))
        except OSError:
            print("Telegram progress message_id could not be saved to disk for the final-notify step")


def update_stage(stage: str) -> None:
    if _state["message_id"] is None:
        return
    _state["current_stage"] = stage
    print(f"Telegram notify: editMessageText (stage={stage})")
    _telegram_request(
        "editMessageText",
        {
            "chat_id": _state["chat_id"],
            "message_id": _state["message_id"],
            "text": _render(),
        },
    )


def mark_stage_done(stage: str) -> None:
    _state["completed"].add(stage)


def is_authorized_user(user_id: int) -> bool:
    """Fixed security rule: only TELEGRAM_ALLOWED_USER_ID may be authorized."""
    allowed = _read_secret_file_optional("TELEGRAM_ALLOWED_USER_ID_FILE")
    if not allowed:
        return False
    try:
        return int(user_id) == int(allowed)
    except (TypeError, ValueError):
        return False


def install_progress_hooks() -> None:
    """Wrap real pipeline stages without changing production semantics."""
    real_build_plan = orchestrator.build_plan
    real_synthesize_wav = orchestrator.synthesize_wav
    real_prepare_clip = orchestrator.prepare_clip
    real_mux = orchestrator.mux
    voice_started = {"flag": False}
    visuals_started = {"flag": False}

    def build_plan_with_progress(*args, **kwargs):
        update_stage("planning")
        result = real_build_plan(*args, **kwargs)
        mark_stage_done("planning")
        return result

    build_plan_with_progress._is_resilient_router = getattr(real_build_plan, "_is_resilient_router", False)

    def synthesize_wav_with_progress(*args, **kwargs):
        if not voice_started["flag"]:
            update_stage("voice")
            voice_started["flag"] = True
        return real_synthesize_wav(*args, **kwargs)

    def prepare_clip_with_progress(*args, **kwargs):
        if not visuals_started["flag"]:
            mark_stage_done("voice")
            update_stage("visuals")
            visuals_started["flag"] = True
        return real_prepare_clip(*args, **kwargs)

    def mux_with_progress(*args, **kwargs):
        mark_stage_done("visuals")
        update_stage("mux")
        result = real_mux(*args, **kwargs)
        mark_stage_done("mux")
        return result

    orchestrator.build_plan = build_plan_with_progress
    orchestrator.synthesize_wav = synthesize_wav_with_progress
    orchestrator.prepare_clip = prepare_clip_with_progress
    orchestrator.mux = mux_with_progress
