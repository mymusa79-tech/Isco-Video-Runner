from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

import isco_video_agent.orchestrator as orchestrator

# Stage order shown in the live progress message, in the sequence the real pipeline
# reaches them: Call 1-4 planning, per-section TTS, per-section visual clip prep, then
# the final mux. Best-effort only - a Telegram outage must never fail real production,
# so every function here swallows its own exceptions.
_STAGES = [
    ("planning", "التخطيط"),
    ("voice", "الصوت"),
    ("visuals", "المشاهد"),
    ("mux", "التجميع"),
]

_state = {"token": "", "chat_id": "", "message_id": None, "completed": set()}


def _read_secret_file_optional(env_name: str) -> str:
    """Unlike task_level_planner_router._read_secret_file, this never raises: live
    progress is optional, missing/absent Telegram secrets just mean it stays off."""
    path = os.environ.get(env_name, "").strip()
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _telegram_request(method: str, payload: dict) -> dict | None:
    if not _state["token"]:
        return None
    url = f"https://api.telegram.org/bot{_state['token']}/{method}"
    data = urllib.parse.urlencode(payload).encode("utf-8")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _render() -> str:
    parts = []
    for key, label in _STAGES:
        if key in _state["completed"]:
            emoji = "✅"
        elif key == _state.get("current_stage"):
            emoji = "🔵"
        else:
            emoji = "⏳"
        parts.append(f"{label}... {emoji}")
    return "🔵 الإنتاج جارٍ...\n" + " | ".join(parts)


def start_progress() -> None:
    """Sends the initial progress message and records its message_id to a file so the
    workflow's separate 'Notify Telegram' bash step can edit this same message for the
    final result instead of sending a new one. No-op if Telegram secrets are absent."""
    token = _read_secret_file_optional("TELEGRAM_BOT_TOKEN_FILE")
    chat_id = _read_secret_file_optional("TELEGRAM_CHAT_ID_FILE")
    _state["token"] = token
    _state["chat_id"] = chat_id
    _state["completed"] = set()
    _state["current_stage"] = None
    if not token or not chat_id:
        return
    resp = _telegram_request("sendMessage", {"chat_id": chat_id, "text": "🔵 بدأ الإنتاج..."})
    if not (resp and resp.get("ok")):
        return
    message_id = resp["result"]["message_id"]
    _state["message_id"] = message_id
    runner_temp = os.environ.get("RUNNER_TEMP", "")
    if runner_temp:
        try:
            open(os.path.join(runner_temp, "telegram-progress-message-id.txt"), "w", encoding="utf-8").write(str(message_id))
        except OSError:
            pass


def update_stage(stage: str) -> None:
    if _state["message_id"] is None:
        return
    _state["current_stage"] = stage
    _telegram_request("editMessageText", {
        "chat_id": _state["chat_id"],
        "message_id": _state["message_id"],
        "text": _render(),
    })


def mark_stage_done(stage: str) -> None:
    _state["completed"].add(stage)


def is_authorized_user(user_id: int) -> bool:
    """Fixed security rule for future command-receiving development (bot commands are
    not implemented yet - nothing calls this today). Only the operator identified by
    TELEGRAM_ALLOWED_USER_ID may ever be treated as authorized; any future feature that
    reacts to incoming Telegram messages/commands must gate on this before acting."""
    allowed = _read_secret_file_optional("TELEGRAM_ALLOWED_USER_ID_FILE")
    if not allowed:
        return False
    try:
        return int(user_id) == int(allowed)
    except (TypeError, ValueError):
        return False


def install_progress_hooks() -> None:
    """Wraps orchestrator.build_plan/synthesize_wav/prepare_clip/mux to report the four
    pipeline stages via update_stage()/mark_stage_done(). Installed last, after the
    router/product-proof/voice-mesh wrappers, so it wraps whatever those already
    installed - same layered-wrapper pattern as install_router() and
    install_voice_mesh()."""
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
