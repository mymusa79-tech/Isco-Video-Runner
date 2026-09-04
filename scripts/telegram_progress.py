from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import isco_video_agent.orchestrator as orchestrator

from scripts import telegram_operations_ui as ops_ui

# Stage order follows the real pipeline: planning, TTS, visual clip prep, final mux.
# Best-effort only - a Telegram/GitHub observability outage must never fail real
# production, so every external progress operation is isolated from production.
_STAGES = list(ops_ui.STAGE_LABELS.items())
_STAGE_KEYS = [key for key, _ in _STAGES]
_PROGRESS_REF = "control-plane-state"
_PROGRESS_RELATIVE_PATH = Path("state") / "production-progress.json"
_PROGRESS_QUEUE: queue.Queue[dict[str, object]] = queue.Queue(maxsize=8)
_PROGRESS_WORKER_LOCK = threading.Lock()
_progress_worker_started = False

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


def _progress_reply_markup() -> str:
    stage = str(_state.get("current_stage") or "")
    callback = f"cmd:progress_stage:{stage}" if stage in _STAGE_KEYS else "cmd:status"
    return json.dumps(
        {
            "inline_keyboard": [
                [{"text": "🔄 تفاصيل المرحلة", "callback_data": callback}],
            ]
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _sanitized_progress_payload(stage: str) -> dict[str, object]:
    completed = [key for key in _STAGE_KEYS if key in _state["completed"]]
    return {
        "schema_version": 1,
        "run_id": str(_state.get("run_id") or ""),
        "run_number": str(_state.get("run_number") or ""),
        "workflow_path": ".github/workflows/produce-resilient-v4.yml",
        "status": "running",
        "stage": stage,
        "completed_stages": completed,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _run_git(root: Path, *args: str, timeout: int = 12) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _write_progress_file(root: Path, payload: dict[str, object]) -> Path:
    target = root / _PROGRESS_RELATIVE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return target


def _commit_progress_payload(payload: dict[str, object]) -> None:
    workspace = (os.environ.get("GITHUB_WORKSPACE") or "").strip()
    run_id = str(payload.get("run_id") or "").strip()
    if not workspace or not run_id.isdigit():
        return
    root = Path(workspace) / "control-writer"
    if not root.is_dir():
        return

    try:
        # Fast path: the dedicated state checkout normally remains current throughout
        # one production run. A bounded repair path handles a concurrent control-plane
        # state commit without ever making observability authoritative for production.
        for attempt in range(2):
            if attempt:
                fetched = _run_git(root, "fetch", "--quiet", "origin", _PROGRESS_REF)
                if fetched.returncode != 0:
                    break
                reset = _run_git(root, "reset", "--hard", f"origin/{_PROGRESS_REF}")
                if reset.returncode != 0:
                    break

            target = _write_progress_file(root, payload)
            added = _run_git(root, "add", target.relative_to(root).as_posix())
            if added.returncode != 0:
                break
            changed = _run_git(root, "diff", "--cached", "--quiet")
            if changed.returncode == 0:
                return
            stage = str(payload.get("stage") or "unknown")
            committed = _run_git(root, "commit", "-m", f"state: production progress {stage} run {run_id}")
            if committed.returncode != 0:
                break
            pushed = _run_git(root, "push", "origin", f"HEAD:{_PROGRESS_REF}", timeout=15)
            if pushed.returncode == 0:
                print(f"Telegram live progress persisted: stage={stage} run_id={run_id}")
                return
        print(f"Telegram live progress persistence skipped: run_id={run_id}")
    except Exception as exc:
        print(f"Telegram live progress persistence skipped ({type(exc).__name__})")


def _progress_worker() -> None:
    while True:
        payload = _PROGRESS_QUEUE.get()
        try:
            _commit_progress_payload(payload)
        finally:
            _PROGRESS_QUEUE.task_done()


def _ensure_progress_worker() -> None:
    global _progress_worker_started
    if _progress_worker_started:
        return
    with _PROGRESS_WORKER_LOCK:
        if _progress_worker_started:
            return
        thread = threading.Thread(target=_progress_worker, name="telegram-live-progress", daemon=True)
        thread.start()
        _progress_worker_started = True


def _enqueue_progress_snapshot(stage: str) -> None:
    workspace = (os.environ.get("GITHUB_WORKSPACE") or "").strip()
    run_id = str(_state.get("run_id") or "").strip()
    if not workspace or not run_id.isdigit() or not (Path(workspace) / "control-writer").is_dir():
        return
    _ensure_progress_worker()
    try:
        _PROGRESS_QUEUE.put_nowait(_sanitized_progress_payload(stage))
    except queue.Full:
        print("Telegram live progress queue full; skipping one observability snapshot")


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
    _enqueue_progress_snapshot("starting")
    if not token or not chat_id:
        print("Telegram progress tracking disabled: bot token or chat id not configured")
        return
    print("Telegram notify: sendMessage (initial lifecycle message)")
    resp = _telegram_request(
        "sendMessage",
        {"chat_id": chat_id, "text": _render(), "reply_markup": _progress_reply_markup()},
    )
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
    if stage not in _STAGE_KEYS:
        return
    _state["current_stage"] = stage
    _enqueue_progress_snapshot(stage)
    if _state["message_id"] is None:
        return
    print(f"Telegram notify: editMessageText (stage={stage})")
    _telegram_request(
        "editMessageText",
        {
            "chat_id": _state["chat_id"],
            "message_id": _state["message_id"],
            "text": _render(),
            "reply_markup": _progress_reply_markup(),
        },
    )


def mark_stage_done(stage: str) -> None:
    if stage not in _STAGE_KEYS:
        return
    _state["completed"].add(stage)
    # Persist the completion boundary itself. The compact lifecycle message remains
    # unchanged, while the opt-in detailed Telegram view can now distinguish the
    # gap after planning from planning-in-progress (Editorial QA), and the gap after
    # mux from rendering-in-progress (Final QC). This is observability-only and
    # remains best-effort/non-authoritative for production.
    if _state.get("current_stage") == stage:
        _enqueue_progress_snapshot(stage)


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