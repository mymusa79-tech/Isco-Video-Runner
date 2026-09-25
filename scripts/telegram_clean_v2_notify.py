from __future__ import annotations

import argparse
import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

MILESTONES = (
    ("planning", "التخطيط"),
    ("script", "النص"),
    ("voice", "الصوت"),
    ("visuals", "المشاهد"),
    ("render", "المونتاج"),
    ("final_master_qc", "الفحص النهائي"),
)
TERMINAL = {"pass", "failed", "quality_pending"}
RUNTIME_BRANCH = "clean-v2-telegram-runtime-state"
RUNTIME_PATH = "state/telegram-runtime.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _telegram_target() -> tuple[str, str]:
    return (
        str(os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip(),
        str(os.environ.get("TELEGRAM_CHAT_ID") or "").strip(),
    )


def build_message_payload(
    text: str,
    *,
    chat_id: str,
    button_text: str = "",
    button_url: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": str(text)[:3900],
        "disable_web_page_preview": True,
    }
    if button_text and button_url:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": button_text, "url": button_url}]]
        }
    return payload


def send_message(
    text: str,
    *,
    button_text: str = "",
    button_url: str = "",
) -> bool:
    token, chat_id = _telegram_target()
    if not token or not chat_id:
        print("Telegram Clean V2 notify disabled: missing bot token or chat id")
        return False
    payload = json.dumps(
        build_message_payload(
            text,
            chat_id=chat_id,
            button_text=button_text,
            button_url=button_url,
        ),
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            body = json.loads(response.read().decode("utf-8"))
        return bool(isinstance(body, dict) and body.get("ok"))
    except Exception as exc:
        print(f"Telegram Clean V2 notify failed: {type(exc).__name__}")
        return False


def _runtime_request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    token = str(os.environ.get("GITHUB_RUNTIME_TOKEN") or "").strip()
    repo = str(os.environ.get("GITHUB_REPOSITORY") or "").strip()
    api = str(os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    if not token or not repo:
        return {}
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{api}/repos/{repo}/{path.lstrip('/')}",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "isco-clean-v2-telegram-runtime",
        },
        method=method,
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def publish_runtime_status(status: dict[str, Any]) -> bool:
    token = str(os.environ.get("GITHUB_RUNTIME_TOKEN") or "").strip()
    repo = str(os.environ.get("GITHUB_REPOSITORY") or "").strip()
    head_sha = str(os.environ.get("GITHUB_SHA") or "").strip()
    if not token or not repo or not head_sha:
        return False
    try:
        try:
            _runtime_request("GET", f"git/ref/heads/{RUNTIME_BRANCH}")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            _runtime_request(
                "POST",
                "git/refs",
                {"ref": f"refs/heads/{RUNTIME_BRANCH}", "sha": head_sha},
            )
        current_sha = ""
        current_data: dict[str, Any] = {}
        try:
            current = _runtime_request(
                "GET",
                f"contents/{RUNTIME_PATH}?ref={RUNTIME_BRANCH}",
            )
            current_sha = str(current.get("sha") or "")
            encoded = str(current.get("content") or "").replace("\n", "")
            if encoded:
                decoded = json.loads(base64.b64decode(encoded).decode("utf-8"))
                if isinstance(decoded, dict):
                    current_data = decoded
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
        merged = {**current_data, **status}
        payload: dict[str, Any] = {
            "message": "state: Telegram runtime status",
            "content": base64.b64encode(
                (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            ).decode("ascii"),
            "branch": RUNTIME_BRANCH,
        }
        if current_sha:
            payload["sha"] = current_sha
        _runtime_request("PUT", f"contents/{RUNTIME_PATH}", payload)
        return True
    except Exception as exc:
        print(f"Telegram runtime status update failed: {type(exc).__name__}")
        return False


def runtime_status_payload(
    *,
    active: bool,
    scope: str,
    kind: str,
    topic: str,
    stage: str,
    run_url: str,
    result: str = "",
) -> dict[str, Any]:
    return {
        "active": active,
        "scope": scope,
        "kind": kind,
        "topic": topic,
        "stage": stage,
        "run_url": run_url,
        "result": result,
        "updated_at": int(time.time()),
    }


def passed_stages(manifest: dict[str, Any]) -> set[str]:
    rows = manifest.get("stages")
    if not isinstance(rows, list):
        return set()
    return {
        str(row.get("name") or "")
        for row in rows
        if isinstance(row, dict) and row.get("status") == "pass"
    }


def milestone_messages(
    manifest: dict[str, Any],
    sent: set[str],
    *,
    kind: str,
) -> list[tuple[str, str]]:
    passed = passed_stages(manifest)
    prefix = "🎙️ خارج النص" if kind == "podcast" else ("🎬 الطويل" if kind == "long" else "⚡ الشورت")
    result: list[tuple[str, str]] = []
    total = len(MILESTONES)
    for index, (stage, label) in enumerate(MILESTONES, 1):
        if stage in passed and stage not in sent:
            result.append((stage, f"{prefix} · {index}/{total} {label} ✅"))
    return result


def current_stage(manifest: dict[str, Any]) -> str:
    rows = manifest.get("stages")
    if not isinstance(rows, list):
        return "لم يبدأ المسار"
    for row in reversed(rows):
        if isinstance(row, dict) and row.get("status") in {"failed", "blocked", "running"}:
            return str(row.get("name") or "غير معروف")
    for row in reversed(rows):
        if isinstance(row, dict) and row.get("status") == "pass":
            return str(row.get("name") or "غير معروف")
    return "لم يبدأ المسار"


def short_failure_reason(manifest: dict[str, Any], job_status: str) -> str:
    rows = manifest.get("stages")
    if isinstance(rows, list):
        for row in reversed(rows):
            if not isinstance(row, dict) or row.get("status") not in {"failed", "blocked"}:
                continue
            stage = str(row.get("name") or "unknown")
            error_type = str(row.get("error_type") or "").strip()
            classification = str(row.get("failure_classification") or "").strip()
            if stage == "voice":
                voice = manifest.get("voice_failure")
                if isinstance(voice, dict):
                    primary = str(voice.get("charon_reason") or "").strip()
                    secondary = str(voice.get("secondary_reason") or "").strip()
                    detail = " / ".join(x for x in (primary, secondary) if x and x != "unavailable")
                    if detail:
                        return f"Voice infrastructure: {detail}"[:300]
            detail = " · ".join(x for x in (error_type, classification) if x)
            return (detail or f"توقف عند {stage}")[:300]
    if str(manifest.get("status") or "") == "quality_pending":
        pending = str(manifest.get("quality_pending_stage") or current_stage(manifest))
        return f"Quality gate لم يمر: {pending}"
    if str(manifest.get("status") or "") == "pass" and job_status != "success":
        return "فشل تحقق Workflow بعد اكتمال مسار الإنتاج"
    return "راجع GitHub Run للتفاصيل التقنية"


def failure_guidance(manifest: dict[str, Any], job_status: str) -> str:
    rows = manifest.get("stages")
    failed = None
    if isinstance(rows, list):
        failed = next(
            (
                row
                for row in reversed(rows)
                if isinstance(row, dict) and row.get("status") in {"failed", "blocked"}
            ),
            None,
        )
    classification = str((failed or {}).get("failure_classification") or "").casefold()
    stage = str((failed or {}).get("name") or "").casefold()
    error_type = str((failed or {}).get("error_type") or "").casefold()
    if classification == "infrastructure" or stage == "voice":
        return "مشكلة مؤقتة في الخدمة أو المزوّد. انتظر قليلًا ثم أعد المحاولة."
    if any(token in classification + " " + error_type for token in ("quality", "content", "validation", "factual")):
        return "المحتوى لم يجتز الفحص. ابدأ بحثًا جديدًا أو اختر موضوعًا آخر."
    if str(manifest.get("status") or "") == "pass" and job_status != "success":
        return "الإنتاج نفسه اكتمل، لكن خطوة لاحقة في GitHub تعثرت. افتح التفاصيل التقنية."
    return "أعد المحاولة مرة واحدة. إذا تكرر الفشل، افتح التفاصيل التقنية."


def terminal_text(*, manifest: dict[str, Any], job_status: str, kind: str, run_url: str) -> str:
    label = "Outside Text" if kind == "podcast" else ("Short" if kind == "short" else "Long")
    manifest_status = str(manifest.get("status") or "")
    stage = current_stage(manifest)
    topic = str(manifest.get("topic") or "").strip()
    success = job_status == "success" and manifest_status == "pass"
    lines = [f"{'✅' if success else '❌'} Clean V2 {label} — {'نجح' if success else 'فشل'}"]
    if topic:
        lines.extend(["", f"الموضوع: {topic}"])
    lines.append(f"آخر مرحلة: {stage}")
    if not success:
        lines.append(f"السبب المختصر: {short_failure_reason(manifest, job_status)}")
        lines.append(f"الخطوة التالية: {failure_guidance(manifest, job_status)}")
    if run_url:
        lines.extend(["", f"GitHub Run: {run_url}"])
    return "\n".join(lines)


def watch(
    output: Path,
    *,
    kind: str,
    poll_seconds: float,
    scope: str = "",
    topic: str = "",
    run_url: str = "",
) -> int:
    manifest_path = output / "run-manifest.json"
    sent: set[str] = set()
    while True:
        manifest = _read_json(manifest_path)
        if manifest:
            for stage, text in milestone_messages(manifest, sent, kind=kind):
                send_message(text)
                publish_runtime_status(
                    runtime_status_payload(
                        active=True,
                        scope=scope or kind,
                        kind=kind,
                        topic=topic,
                        stage=text,
                        run_url=run_url,
                    )
                )
                sent.add(stage)
            if str(manifest.get("status") or "") in TERMINAL:
                return 0
        time.sleep(max(0.5, poll_seconds))


def terminal(output: Path, *, job_status: str, kind: str, run_url: str) -> int:
    manifest = _read_json(output / "run-manifest.json")
    ok = send_message(
        terminal_text(
            manifest=manifest,
            job_status=job_status,
            kind=kind,
            run_url=run_url,
        )
    )
    return 0 if ok else 1


def started_text(*, scope: str, topic: str, run_url: str) -> str:
    label = {"long": "🎬 فيديو طويل", "short": "⚡ شورت", "bundle": "🎬 طويل + ⚡ شورت", "podcast": "🎙️ خارج النص"}.get(scope, "الإنتاج")
    lines = [
        f"🚀 بدأ الإنتاج فعليًا — {label}",
        f"الموضوع: {topic}" if topic else "الموضوع: غير محدد",
        "سأرسل لك تحديثًا عند اكتمال كل مرحلة رئيسية.",
    ]
    if run_url:
        lines.extend(["", f"متابعة التشغيل: {run_url}"])
    return "\n".join(lines)


def artifact_delivery_text(*, scope: str, topic: str) -> str:
    label = {"long": "الفيديو الطويل", "short": "الشورت", "bundle": "الحزمة", "podcast": "خارج النص"}.get(scope, "الإنتاج")
    lines = [f"🎥 {label} جاهز للتسليم"]
    if topic:
        lines.append(f"الموضوع: {topic}")
    lines.append("اضغط الزر لفتح ملفات الإنتاج النهائية.")
    return "\n".join(lines)


def bundle_blocked_text(*, topic: str, run_url: str) -> str:
    lines = [
        "⚠️ توقفت الحزمة بعد فشل الفيديو الطويل",
        "⚡ الشورت لم يبدأ لأن الحزمة تشترط نجاح الطويل أولًا.",
        "الخطوة التالية: عالج أو أعد محاولة الفيديو الطويل، ثم أعد تشغيل الحزمة.",
    ]
    if topic:
        lines.extend(["", f"الموضوع: {topic}"])
    if run_url:
        lines.extend(["", f"تفاصيل التشغيل: {run_url}"])
    return "\n".join(lines)


def bundle_summary_text(*, topic: str, run_url: str) -> str:
    lines = [
        "✅ اكتملت الحزمة كاملة",
        "",
        "🎬 الفيديو الطويل: مكتمل",
        "⚡ الشورت: مكتمل",
    ]
    if topic:
        lines.extend(["", f"الموضوع: {topic}"])
    if run_url:
        lines.extend(["", f"تفاصيل التشغيل: {run_url}"])
    return "\n".join(lines)


def workflow_watchdog_text(*, scope: str, run_url: str) -> str:
    label = {"long": "الفيديو الطويل", "short": "الشورت", "bundle": "الطويل + الشورت", "podcast": "البودكاست"}.get(scope, "الإنتاج")
    lines = [
        f"❌ تعذر إكمال {label}",
        "",
        "توقف التشغيل قبل أن يصل إلى مرحلة إنتاج يمكن تشخيصها من ملف الفيديو.",
        "الخطوة التالية: أعد المحاولة مرة واحدة. إذا تكرر الفشل، افتح التفاصيل التقنية.",
    ]
    if run_url:
        lines.extend(["", f"تفاصيل التشغيل: {run_url}"])
    return "\n".join(lines)


def workflow_watchdog(*, output_root: Path, job_status: str, scope: str, run_url: str) -> int:
    if job_status == "success":
        return 0
    if output_root.exists() and any(output_root.rglob(".telegram-terminal-sent")):
        return 0
    return 0 if send_message(workflow_watchdog_text(scope=scope, run_url=run_url)) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Best-effort Telegram observer for Clean V2")
    sub = parser.add_subparsers(dest="command", required=True)

    watch_p = sub.add_parser("watch")
    watch_p.add_argument("--output", type=Path, required=True)
    watch_p.add_argument("--kind", choices=("long", "short", "podcast"), required=True)
    watch_p.add_argument("--poll-seconds", type=float, default=4.0)
    watch_p.add_argument("--scope", choices=("long", "short", "bundle", "podcast"), default="")
    watch_p.add_argument("--topic", default="")
    watch_p.add_argument("--run-url", default="")

    final_p = sub.add_parser("terminal")
    final_p.add_argument("--output", type=Path, required=True)
    final_p.add_argument("--kind", choices=("long", "short", "podcast"), required=True)
    final_p.add_argument("--job-status", required=True)
    final_p.add_argument("--run-url", default="")

    started_p = sub.add_parser("started")
    started_p.add_argument("--scope", choices=("long", "short", "bundle", "podcast"), required=True)
    started_p.add_argument("--topic", default="")
    started_p.add_argument("--run-url", default="")

    artifact_p = sub.add_parser("artifact")
    artifact_p.add_argument("--scope", choices=("long", "short", "bundle", "podcast"), required=True)
    artifact_p.add_argument("--topic", default="")
    artifact_p.add_argument("--url", required=True)

    blocked_p = sub.add_parser("bundle-blocked")
    blocked_p.add_argument("--topic", default="")
    blocked_p.add_argument("--run-url", default="")

    bundle_p = sub.add_parser("bundle-summary")
    bundle_p.add_argument("--topic", default="")
    bundle_p.add_argument("--run-url", default="")

    watchdog_p = sub.add_parser("watchdog")
    watchdog_p.add_argument("--output-root", type=Path, required=True)
    watchdog_p.add_argument("--scope", choices=("long", "short", "bundle", "podcast"), required=True)
    watchdog_p.add_argument("--job-status", required=True)
    watchdog_p.add_argument("--topic", default="")
    watchdog_p.add_argument("--run-url", default="")

    args = parser.parse_args()
    if args.command == "watch":
        return watch(
            args.output,
            kind=args.kind,
            poll_seconds=args.poll_seconds,
            scope=args.scope,
            topic=args.topic,
            run_url=args.run_url,
        )
    if args.command == "started":
        publish_runtime_status(
            runtime_status_payload(
                active=True,
                scope=args.scope,
                kind="",
                topic=args.topic,
                stage="بدأ الإنتاج",
                run_url=args.run_url,
            )
        )
        return 0 if send_message(
            started_text(scope=args.scope, topic=args.topic, run_url=args.run_url)
        ) else 1
    if args.command == "artifact":
        delivered = send_message(
            artifact_delivery_text(scope=args.scope, topic=args.topic),
            button_text="🎥 فتح الفيديو النهائي",
            button_url=args.url,
        )
        publish_runtime_status(
            {
                **runtime_status_payload(
                    active=False,
                    scope=args.scope,
                    kind="",
                    topic=args.topic,
                    stage="تم التسليم",
                    run_url="",
                    result="success",
                ),
                "last_success": {
                    "scope": args.scope,
                    "topic": args.topic,
                    "artifact_url": args.url,
                    "delivered_at": int(time.time()),
                },
            }
        )
        return 0 if delivered else 1
    if args.command == "bundle-blocked":
        return 0 if send_message(
            bundle_blocked_text(topic=args.topic, run_url=args.run_url)
        ) else 1
    if args.command == "bundle-summary":
        return 0 if send_message(
            bundle_summary_text(topic=args.topic, run_url=args.run_url)
        ) else 1
    if args.command == "watchdog":
        publish_runtime_status(
            runtime_status_payload(
                active=False,
                scope=args.scope,
                kind="",
                topic=getattr(args, "topic", ""),
                stage="اكتمل التشغيل" if args.job_status == "success" else "توقف التشغيل",
                run_url=args.run_url,
                result=args.job_status,
            )
        )
        return workflow_watchdog(
            output_root=args.output_root,
            job_status=args.job_status,
            scope=args.scope,
            run_url=args.run_url,
        )
    return terminal(
        args.output,
        job_status=args.job_status,
        kind=args.kind,
        run_url=args.run_url,
    )


if __name__ == "__main__":
    raise SystemExit(main())
