from __future__ import annotations

import base64
import json
import os
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
OUTBOX_WORKFLOW = "telegram-outbox-send.yml"


class PublishApprovalConfigError(RuntimeError):
    pass


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
        failed_providers = [
            attempt["provider"]
            for attempt in attempts
            if attempt.get("result") not in ("success", "circuit-open")
        ]
        seen: list[str] = []
        for provider in failed_providers:
            if provider not in seen:
                seen.append(provider)
        if seen:
            warnings.append("تم اللجوء لمزوّد احتياطي بعد فشل: " + "، ".join(seen) + ".")
    return warnings


def build_caption(
    quality: dict,
    plan: dict,
    warnings: list[str],
    candidate: ReleaseCandidateDigest,
    run_url: str,
) -> str:
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
    lines.extend(
        [
            "",
            f"🔐 بصمة النسخة: {candidate.digest[:12]}",
            f"🔎 المراجعة: {run_url}",
            "هل تريد نشر هذه النسخة بالضبط؟",
        ]
    )
    return "\n".join(lines)


def build_outbox_request(
    *,
    candidate: ReleaseCandidateDigest,
    caption: str,
    created_at: str,
) -> dict:
    approval_id = approval_id_for_candidate(candidate)
    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "✅ انشر",
                    "callback_data": callback_data_for(candidate, ApprovalDecision.APPROVED),
                },
                {
                    "text": "❌ لا تنشر",
                    "callback_data": callback_data_for(candidate, ApprovalDecision.REJECTED),
                },
            ]
        ]
    }
    return {
        "schema_version": 1,
        "outbox_message_id": f"release-approval-{approval_id}",
        "message_kind": "release_candidate",
        "correlation_id": candidate.run_id,
        "journal_event_ref": f"release-candidate:{candidate.digest}",
        "created_at": created_at,
        "approval_id": approval_id,
        "candidate_digest": candidate.digest,
        "method": "sendMessage",
        "payload": {"text": caption, "reply_markup": keyboard},
    }


def dispatch_outbox_request(
    repository: str,
    request: dict,
    github_token: str,
    *,
    ref: str = "main",
) -> None:
    token = str(github_token or "").strip()
    if not token:
        raise PublishApprovalConfigError("GITHUB_TOKEN is required to dispatch Telegram outbox")
    encoded = base64.b64encode(
        json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    if len(encoded.encode("ascii")) > 64 * 1024:
        raise RuntimeError("Telegram outbox dispatch request exceeds workflow input budget")
    url = f"https://api.github.com/repos/{repository}/actions/workflows/{OUTBOX_WORKFLOW}/dispatches"
    response = requests.post(
        url,
        headers={
            "authorization": f"Bearer {token}",
            "accept": "application/vnd.github+json",
            "x-github-api-version": "2022-11-28",
            "user-agent": "isco-release-approval-gate",
        },
        json={"ref": ref, "inputs": {"outbox_request_b64": encoded}},
        timeout=20,
    )
    if response.status_code != 204:
        raise RuntimeError(
            f"Telegram outbox workflow dispatch failed: HTTP {response.status_code}: {response.text[:300]}"
        )


def _read_projection(repository: str, github_token: str, nonce: int) -> dict:
    token = str(github_token or "").strip()
    if not token:
        raise PublishApprovalConfigError("GITHUB_TOKEN is required to read private Telegram projection")
    url = f"https://api.github.com/repos/{repository}/contents/state/telegram-status.json"
    response = requests.get(
        url,
        headers={
            "authorization": f"Bearer {token}",
            "accept": "application/vnd.github+json",
            "x-github-api-version": "2022-11-28",
            "cache-control": "no-cache",
            "user-agent": f"isco-release-approval-gate/{nonce}",
        },
        params={"ref": "control-plane-state", "nonce": nonce},
        timeout=20,
    )
    if response.status_code == 404:
        return {}
    response.raise_for_status()
    envelope = response.json()
    if not isinstance(envelope, dict) or envelope.get("encoding") != "base64":
        raise RuntimeError("Telegram status projection GitHub envelope is malformed")
    try:
        raw = base64.b64decode(str(envelope.get("content") or "").replace("\n", ""), validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("Telegram status projection content is invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeError("Telegram status projection is malformed or unsupported")
    return payload


def poll_for_decision(
    repository: str,
    candidate: ReleaseCandidateDigest,
    github_token: str,
    timeout_seconds: int = POLL_TIMEOUT_SECONDS,
) -> dict:
    start = time.monotonic()
    deadline = start + timeout_seconds
    last_log = start
    nonce = 0
    while True:
        now = time.monotonic()
        if now >= deadline:
            break
        nonce += 1
        projection = _read_projection(repository, github_token, nonce)
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


def request_publish_approval(
    *,
    out_dir: Path,
    run_id: str,
    run_url: str,
    repository: str,
    github_token: str,
) -> dict:
    require = os.environ.get("REQUIRE_PUBLISH_APPROVAL", "false").strip().lower() == "true"
    if not require:
        print("Publish approval gate disabled (REQUIRE_PUBLISH_APPROVAL != true)")
        return {"decision": "disabled", "effective_decision": "approved"}

    quality = json.loads((out_dir / "quality-final.json").read_text(encoding="utf-8"))
    plan = json.loads((out_dir / "plan.json").read_text(encoding="utf-8"))
    telemetry_path = out_dir / "planning-telemetry.json"
    telemetry = json.loads(telemetry_path.read_text(encoding="utf-8")) if telemetry_path.exists() else None
    candidate = build_release_candidate(root=out_dir, run_id=run_id)

    warnings = build_warnings(quality, telemetry)
    created_at = datetime.now(timezone.utc).isoformat()
    caption = build_caption(quality, plan, warnings, candidate, run_url)
    request = build_outbox_request(candidate=candidate, caption=caption, created_at=created_at)
    dispatch_ref = os.environ.get("TELEGRAM_OUTBOX_REF", "main").strip() or "main"
    dispatch_outbox_request(repository, request, github_token, ref=dispatch_ref)
    print(
        "Publish approval request queued through durable Telegram outbox: "
        f"approval_id={approval_id_for_candidate(candidate)} candidate={candidate.digest}"
    )

    poll_result = poll_for_decision(repository, candidate, github_token)
    decision = poll_result["decision"]
    effective_decision = decision if decision in {"approved", "rejected"} else effective_decision_after_timeout()
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
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not run_id or not repository:
        raise RuntimeError("Publish approval requires GITHUB_RUN_ID and GITHUB_REPOSITORY")
    run_url = f"https://github.com/{repository}/actions/runs/{run_id}"
    result = request_publish_approval(
        out_dir=out_dir,
        run_id=run_id,
        run_url=run_url,
        repository=repository,
        github_token=github_token,
    )
    print(f"Publish approval decision: {result['decision']} (effective: {result['effective_decision']})")
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"decision={result['decision']}\n")
            handle.write(f"effective_decision={result['effective_decision']}\n")
            if result.get("candidate_digest"):
                handle.write(f"candidate_digest={result['candidate_digest']}\n")
    (out_dir / "publish-decision.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
