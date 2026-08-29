from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from scripts.orchestration_telegram_ingress_outbox import ApprovalDecision, ReleaseCandidateDigest
from scripts.telegram_release_approval import (
    approval_id_for_candidate,
    build_release_candidate,
    callback_data_for,
    decision_from_projection,
    effective_decision_after_timeout,
)

POLL_TIMEOUT_SECONDS = 1800
POLL_INTERVAL_SECONDS = 5
PROGRESS_LOG_INTERVAL_SECONDS = 60


class PublishApprovalConfigError(RuntimeError):
    """Fail-loud configuration error for the release approval gate."""


def _read_secret_file_required(env_name: str) -> str:
    path = os.environ.get(env_name, "").strip()
    if not path:
        raise PublishApprovalConfigError(f"{env_name} is not set")
    try:
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError as exc:
        raise PublishApprovalConfigError(f"{env_name} could not be read: {exc}") from exc
    if not value:
        raise PublishApprovalConfigError(f"{env_name} is empty")
    return value


def _telegram_api(token: str, method: str, payload: dict | None = None, files: dict | None = None) -> dict:
    """Outbound Telegram only. L6 ingress is exclusively the authenticated Edge webhook."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    response = requests.post(url, data=payload or {}, files=files, timeout=35)
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {body.get('description', 'unknown error')}")
    return body["result"]


def _format_duration(seconds: float) -> str:
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


def build_warnings(quality: dict, telemetry: dict | None) -> list[str]:
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


def build_caption(quality: dict, plan: dict, warnings: list[str], candidate: ReleaseCandidateDigest) -> str:
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
        lines.extend(f"- {warning}" for warning in warnings)
    lines.extend(["", f"🔐 بصمة النسخة: {candidate.digest[:12]}", "هل تريد نشر هذه النسخة بالضبط؟"])
    return "\n".join(lines)


def generate_thumbnail(video_path: Path, duration: float, dest: Path) -> Path:
    seek = max(1.0, duration * 0.1)
    subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{seek:.2f}", "-i", str(video_path), "-frames:v", "1", "-vf", "scale=640:-1", str(dest)],
        check=True,
        capture_output=True,
    )
    return dest


def send_approval_request(
    token: str,
    chat_id: str,
    thumbnail_path: Path,
    caption: str,
    candidate: ReleaseCandidateDigest,
) -> int:
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ انشر", "callback_data": callback_data_for(candidate, ApprovalDecision.APPROVED)},
            {"text": "❌ لا تنشر", "callback_data": callback_data_for(candidate, ApprovalDecision.REJECTED)},
        ]]
    }
    with open(thumbnail_path, "rb") as handle:
        result = _telegram_api(
            token,
            "sendPhoto",
            payload={"chat_id": chat_id, "caption": caption, "reply_markup": json.dumps(keyboard)},
            files={"photo": handle},
        )
    return int(result["message_id"])


def _projection_url(repository: str, approval_id: str, nonce: int) -> str:
    return (
        f"https://raw.githubusercontent.com/{repository}/control-plane-state/state/telegram-status.json"
        f"?approval={approval_id}&nonce={nonce}"
    )


def _read_projection(repository: str, approval_id: str, nonce: int) -> dict:
    response = requests.get(
        _projection_url(repository, approval_id, nonce),
        headers={"user-agent": "isco-release-approval-gate", "cache-control": "no-cache"},
        timeout=20,
    )
    if response.status_code == 404:
        return {}
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeError("Telegram status projection is malformed or unsupported")
    return payload


def poll_for_decision(
    repository: str,
    candidate: ReleaseCandidateDigest,
    timeout_seconds: int = POLL_TIMEOUT_SECONDS,
) -> dict:
    approval_id = approval_id_for_candidate(candidate)
    start = time.monotonic()
    deadline = start + timeout_seconds
    last_log = start
    nonce = 0
    while True:
        now = time.monotonic()
        if now >= deadline:
            break
        nonce += 1
        projection = _read_projection(repository, approval_id, nonce)
        decision = decision_from_projection(projection, candidate)
        if decision is ApprovalDecision.APPROVED:
            return {"decision": "approved", "decided_at": datetime.now(timezone.utc).isoformat()}
        if decision is ApprovalDecision.REJECTED:
            return {"decision": "rejected", "decided_at": datetime.now(timezone.utc).isoformat()}
        if now - last_log >= PROGRESS_LOG_INTERVAL_SECONDS:
            elapsed_minutes = int((now - start) / 60)
            total_minutes = int(timeout_seconds / 60)
            print(f"لا يزال بانتظار receipt الموافقة عبر الـWebhook... ({elapsed_minutes}/{total_minutes} دقيقة)")
            last_log = now
        time.sleep(min(POLL_INTERVAL_SECONDS, max(0.1, deadline - time.monotonic())))
    return {"decision": "timeout", "decided_at": datetime.now(timezone.utc).isoformat()}


def finalize_decision(token: str, chat_id: str, message_id: int, decision: str, run_url: str) -> None:
    if decision == "approved":
        status_text = "✅ تم اعتماد هذه النسخة للنشر"
    elif decision == "rejected":
        status_text = "❌ تم رفض هذه النسخة"
    else:
        status_text = "⏱️ انتهت مهلة الموافقة دون قرار — لم يُنشر"
    _telegram_api(
        token,
        "editMessageCaption",
        payload={"chat_id": chat_id, "message_id": message_id, "caption": status_text, "reply_markup": json.dumps({"inline_keyboard": []})},
    )
    if decision != "approved":
        _telegram_api(token, "sendMessage", payload={"chat_id": chat_id, "text": f"{status_text}\nالفيديو لا يزال متاحًا من صفحة الـrun:\n{run_url}"})


def request_publish_approval(*, out_dir: Path, run_id: str, run_url: str, repository: str) -> dict:
    require = os.environ.get("REQUIRE_PUBLISH_APPROVAL", "false").strip().lower() == "true"
    if not require:
        print("Publish approval gate disabled (REQUIRE_PUBLISH_APPROVAL != true)")
        return {"decision": "disabled", "effective_decision": "approved"}

    token = _read_secret_file_required("TELEGRAM_BOT_TOKEN_FILE")
    chat_id = _read_secret_file_required("TELEGRAM_CHAT_ID_FILE")
    _read_secret_file_required("TELEGRAM_ALLOWED_USER_ID_FILE")

    quality = json.loads((out_dir / "quality-final.json").read_text(encoding="utf-8"))
    plan = json.loads((out_dir / "plan.json").read_text(encoding="utf-8"))
    telemetry_path = out_dir / "planning-telemetry.json"
    telemetry = json.loads(telemetry_path.read_text(encoding="utf-8")) if telemetry_path.exists() else None
    candidate = build_release_candidate(root=out_dir, run_id=run_id)

    warnings = build_warnings(quality, telemetry)
    caption = build_caption(quality, plan, warnings, candidate)
    duration = float(quality.get("video_stream_duration") or quality.get("duration") or 0.0)
    thumbnail_path = out_dir / "publish-approval-thumbnail.jpg"
    generate_thumbnail(out_dir / "final.mp4", duration, thumbnail_path)

    message_id = send_approval_request(token, chat_id, thumbnail_path, caption, candidate)
    print(
        "Publish approval request sent through outbound Telegram API: "
        f"message_id={message_id} approval_id={approval_id_for_candidate(candidate)} candidate={candidate.digest}"
    )

    poll_result = poll_for_decision(repository, candidate)
    decision = poll_result["decision"]
    effective_decision = decision if decision in {"approved", "rejected"} else effective_decision_after_timeout()
    finalize_decision(token, chat_id, message_id, decision, run_url)
    return {
        **poll_result,
        "effective_decision": effective_decision,
        "approval_id": approval_id_for_candidate(candidate),
        "candidate_digest": candidate.digest,
        "release_candidate": {
            "run_id": candidate.run_id,
            "final_mp4_sha256": candidate.final_mp4_sha256,
            "delivery_manifest_sha256": candidate.delivery_manifest_sha256,
            "capability_manifest_sha256": candidate.capability_manifest_sha256,
            "release_asset_set_digest": candidate.release_asset_set_digest,
        },
    }


def _latest_output_dir() -> Path | None:
    roots = sorted(Path("output").glob("*"), key=lambda path: path.stat().st_mtime, reverse=True)
    return roots[0] if roots else None


def main() -> None:
    out_dir = _latest_output_dir()
    if out_dir is None:
        raise RuntimeError("No production output directory found for the publish approval gate")
    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not run_id or not repository:
        raise RuntimeError("Publish approval requires GITHUB_RUN_ID and GITHUB_REPOSITORY")
    run_url = f"https://github.com/{repository}/actions/runs/{run_id}"
    result = request_publish_approval(out_dir=out_dir, run_id=run_id, run_url=run_url, repository=repository)
    print(f"Publish approval decision: {result['decision']} (effective: {result['effective_decision']})")
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"decision={result['decision']}\n")
            handle.write(f"effective_decision={result['effective_decision']}\n")
            if result.get("candidate_digest"):
                handle.write(f"candidate_digest={result['candidate_digest']}\n")
    (out_dir / "publish-decision.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
