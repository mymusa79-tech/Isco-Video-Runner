from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from scripts.telegram_progress import is_authorized_user

# Prefixes embedded in each inline-keyboard callback_data alongside the GitHub Actions
# run_id (e.g. "approve:31905192579"), so a stale button from an old chat message can
# never be mistaken for a decision on the current run.
APPROVE_PREFIX = "approve"
REJECT_PREFIX = "reject"

POLL_TIMEOUT_SECONDS = 1800  # 30 minutes, per the approved design.
POLL_LONG_POLL_SECONDS = 25  # Telegram getUpdates server-side long-poll window.
PROGRESS_LOG_INTERVAL_SECONDS = 60


class PublishApprovalConfigError(RuntimeError):
    """Raised only when REQUIRE_PUBLISH_APPROVAL=true but Telegram secrets are missing
    or unreadable. Must fail loud (block the release) rather than silently proceed to
    publish - a silent bypass here would defeat the entire point of the gate without
    anyone noticing."""


def _read_secret_file_required(env_name: str) -> str:
    path = os.environ.get(env_name, "").strip()
    if not path:
        raise PublishApprovalConfigError(f"{env_name} is not set")
    try:
        with open(path, encoding="utf-8") as f:
            value = f.read().strip()
    except OSError as exc:
        raise PublishApprovalConfigError(f"{env_name} could not be read: {exc}") from exc
    if not value:
        raise PublishApprovalConfigError(f"{env_name} is empty")
    return value


def _telegram_api(token: str, method: str, payload: dict | None = None, files: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    resp = requests.post(url, data=payload or {}, files=files, timeout=35)
    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {body.get('description', 'unknown error')}")
    return body["result"]


def _format_duration(seconds: float) -> str:
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


def build_warnings(quality: dict, telemetry: dict | None) -> list[str]:
    """The three warning rules agreed in the design: fallback content, an av-sync delta
    close to the gate's own threshold, and a non-trivial provider failover chain."""
    warnings: list[str] = []

    plan_source = str(quality.get("plan_source") or "")
    if "product_proof_fallback" in plan_source:
        warnings.append("استُخدم محتوى احتياطي ثابت (fallback)، وليس تخطيطًا سحابيًا جديدًا لهذا الموضوع.")

    av_delta = quality.get("av_delta_seconds")
    max_delta = quality.get("av_sync_max_delta_seconds")
    if isinstance(av_delta, (int, float)) and isinstance(max_delta, (int, float)) and max_delta > 0:
        if av_delta > max_delta * 0.5:
            warnings.append(f"فرق تزامن الصوت/الفيديو قريب من الحد المسموح ({av_delta:.2f}ث من أصل {max_delta:.2f}ث).")

    if telemetry:
        attempts = telemetry.get("attempts", [])
        failed_providers = [a["provider"] for a in attempts if a.get("result") not in ("success", "circuit-open")]
        seen: list[str] = []
        for provider in failed_providers:
            if provider not in seen:
                seen.append(provider)
        if seen:
            warnings.append("تم اللجوء لمزوّد احتياطي بعد فشل: " + "، ".join(seen) + ".")

    return warnings


def build_caption(quality: dict, plan: dict, warnings: list[str]) -> str:
    lines = ["🎬 مراجعة قبل النشر", ""]

    duration = quality.get("video_stream_duration") or quality.get("duration")
    if isinstance(duration, (int, float)):
        lines.append(f"المدة: {_format_duration(duration)}")

    topic = plan.get("topic")
    if topic:
        lines.append(f"الموضوع: {topic}")

    plan_source = quality.get("plan_source") or plan.get("plan_source")
    if plan_source:
        lines.append(f"مصدر التخطيط: {plan_source}")

    if warnings:
        lines.append("")
        lines.append("⚠️ تحذيرات:")
        lines.extend(f"- {w}" for w in warnings)

    lines.append("")
    lines.append("هل تريد نشر هذا الفيديو؟")
    return "\n".join(lines)


def generate_thumbnail(video_path: Path, duration: float, dest: Path) -> Path:
    """A frame at 10% of the video's duration, never before 1s, to avoid a black
    first frame while still reading as representative of the actual content."""
    seek = max(1.0, duration * 0.1)
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", f"{seek:.2f}",
            "-i", str(video_path),
            "-frames:v", "1",
            "-vf", "scale=640:-1",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    return dest


def send_approval_request(token: str, chat_id: str, thumbnail_path: Path, caption: str, run_id: str) -> int:
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ انشر", "callback_data": f"{APPROVE_PREFIX}:{run_id}"},
            {"text": "❌ لا تنشر", "callback_data": f"{REJECT_PREFIX}:{run_id}"},
        ]]
    }
    with open(thumbnail_path, "rb") as f:
        result = _telegram_api(
            token, "sendPhoto",
            payload={"chat_id": chat_id, "caption": caption, "reply_markup": json.dumps(keyboard)},
            files={"photo": f},
        )
    return result["message_id"]


def _prime_offset(token: str) -> int:
    """Reads whatever updates are already queued and discards them, so stale button
    clicks left over from earlier chat history are never mistaken for a fresh answer
    to this run's approval request."""
    updates = _telegram_api(token, "getUpdates", payload={"timeout": 0})
    if not updates:
        return 0
    return max(u["update_id"] for u in updates) + 1


def _handle_update(token: str, update: dict, message_id: int, run_id: str) -> dict | None:
    callback = update.get("callback_query")
    if not callback:
        return None
    if callback.get("message", {}).get("message_id") != message_id:
        return None

    data = callback.get("data", "")
    if data == f"{APPROVE_PREFIX}:{run_id}":
        decision = "approved"
    elif data == f"{REJECT_PREFIX}:{run_id}":
        decision = "rejected"
    else:
        return None

    user_id = (callback.get("from") or {}).get("id")
    if not is_authorized_user(user_id):
        _telegram_api(token, "answerCallbackQuery", payload={
            "callback_query_id": callback["id"],
            "text": "غير مصرح لك بهذا القرار",
            "show_alert": "true",
        })
        return None

    _telegram_api(token, "answerCallbackQuery", payload={"callback_query_id": callback["id"]})
    return {
        "decision": decision,
        "decided_by": user_id,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }


def poll_for_decision(token: str, message_id: int, run_id: str, timeout_seconds: int = POLL_TIMEOUT_SECONDS) -> dict:
    offset = _prime_offset(token)
    start = time.monotonic()
    deadline = start + timeout_seconds
    last_log = start

    while True:
        now = time.monotonic()
        if now >= deadline:
            break

        remaining = deadline - now
        poll_timeout = int(min(POLL_LONG_POLL_SECONDS, max(1, remaining)))
        updates = _telegram_api(token, "getUpdates", payload={
            "offset": offset,
            "timeout": poll_timeout,
            "allowed_updates": json.dumps(["callback_query"]),
        })
        for update in updates:
            offset = max(offset, update["update_id"] + 1)
            result = _handle_update(token, update, message_id, run_id)
            if result is not None:
                return result

        if now - last_log >= PROGRESS_LOG_INTERVAL_SECONDS:
            elapsed_minutes = int((now - start) / 60)
            total_minutes = int(timeout_seconds / 60)
            print(f"لا يزال بانتظار الرد على طلب النشر... ({elapsed_minutes}/{total_minutes} دقيقة)")
            last_log = now

    return {"decision": "timeout", "decided_by": None, "decided_at": datetime.now(timezone.utc).isoformat()}


def finalize_decision(token: str, chat_id: str, message_id: int, decision: str, effective_decision: str, run_url: str) -> None:
    if effective_decision == "approved":
        status_text = "✅ تم اعتماد النشر" if decision == "approved" else "✅ تم النشر تلقائيًا بعد انتهاء المهلة (٣٠ دقيقة بلا رد)"
    else:
        status_text = "❌ تم إلغاء النشر" if decision == "rejected" else "⏱️ انتهت المهلة (٣٠ دقيقة) دون رد - لم يُنشر"

    _telegram_api(token, "editMessageCaption", payload={
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": status_text,
        "reply_markup": json.dumps({"inline_keyboard": []}),
    })

    if effective_decision != "approved":
        _telegram_api(token, "sendMessage", payload={
            "chat_id": chat_id,
            "text": f"{status_text}\nالفيديو لا يزال متاحًا للتحميل من صفحة الـrun:\n{run_url}",
        })


def request_publish_approval(*, out_dir: Path, run_id: str, run_url: str) -> dict:
    require = os.environ.get("REQUIRE_PUBLISH_APPROVAL", "false").strip().lower() == "true"
    if not require:
        print("Publish approval gate disabled (REQUIRE_PUBLISH_APPROVAL != true)")
        return {"decision": "disabled", "effective_decision": "approved"}

    token = _read_secret_file_required("TELEGRAM_BOT_TOKEN_FILE")
    chat_id = _read_secret_file_required("TELEGRAM_CHAT_ID_FILE")
    _read_secret_file_required("TELEGRAM_ALLOWED_USER_ID_FILE")  # is_authorized_user() reads this itself below;
    # this call only asserts presence up front so a missing allow-list fails loud here,
    # not silently later when the first (or no) reply ever arrives.

    quality = json.loads((out_dir / "quality-final.json").read_text(encoding="utf-8"))
    plan = json.loads((out_dir / "plan.json").read_text(encoding="utf-8"))
    telemetry_path = out_dir / "planning-telemetry.json"
    telemetry = json.loads(telemetry_path.read_text(encoding="utf-8")) if telemetry_path.exists() else None

    warnings = build_warnings(quality, telemetry)
    caption = build_caption(quality, plan, warnings)

    duration = float(quality.get("video_stream_duration") or quality.get("duration") or 0.0)
    thumbnail_path = out_dir / "publish-approval-thumbnail.jpg"
    generate_thumbnail(out_dir / "final.mp4", duration, thumbnail_path)

    message_id = send_approval_request(token, chat_id, thumbnail_path, caption, run_id)
    print(f"Publish approval request sent: message_id={message_id}")

    poll_result = poll_for_decision(token, message_id, run_id)
    decision = poll_result["decision"]

    timeout_action = os.environ.get("PUBLISH_APPROVAL_TIMEOUT_ACTION", "hold").strip().lower()
    if decision == "timeout":
        effective_decision = "approved" if timeout_action == "publish" else "rejected"
    else:
        effective_decision = decision

    finalize_decision(token, chat_id, message_id, decision, effective_decision, run_url)

    return {**poll_result, "effective_decision": effective_decision}


def _latest_output_dir() -> Path | None:
    roots = sorted(Path("output").glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return roots[0] if roots else None


def main() -> None:
    out_dir = _latest_output_dir()
    if out_dir is None:
        raise RuntimeError("No production output directory found for the publish approval gate")

    run_id = os.environ.get("GITHUB_RUN_ID", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_url = f"https://github.com/{repository}/actions/runs/{run_id}"

    result = request_publish_approval(out_dir=out_dir, run_id=run_id, run_url=run_url)
    print(f"Publish approval decision: {result['decision']} (effective: {result['effective_decision']})")

    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"decision={result['decision']}\n")
            f.write(f"effective_decision={result['effective_decision']}\n")

    (out_dir / "publish-decision.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
