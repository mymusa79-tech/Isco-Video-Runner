from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

MILESTONES = (
    ("planning", "Planning"),
    ("script", "Script"),
    ("voice", "Voice"),
    ("visuals", "Visuals"),
    ("render", "Render"),
    ("final_master_qc", "Final Master"),
)
TERMINAL = {"pass", "failed", "quality_pending"}


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


def send_message(text: str) -> bool:
    token, chat_id = _telegram_target()
    if not token or not chat_id:
        print("Telegram Clean V2 notify disabled: missing bot token or chat id")
        return False
    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": str(text)[:3900],
            "disable_web_page_preview": True,
        },
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


def passed_stages(manifest: dict[str, Any]) -> set[str]:
    rows = manifest.get("stages")
    if not isinstance(rows, list):
        return set()
    return {
        str(row.get("name") or "")
        for row in rows
        if isinstance(row, dict) and row.get("status") == "pass"
    }


def milestone_messages(manifest: dict[str, Any], sent: set[str]) -> list[tuple[str, str]]:
    passed = passed_stages(manifest)
    result: list[tuple[str, str]] = []
    for stage, label in MILESTONES:
        if stage in passed and stage not in sent:
            result.append((stage, f"✅ Clean V2 · {label} تم"))
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
    label = "Short" if kind == "short" else "Long"
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


def watch(output: Path, *, kind: str, poll_seconds: float) -> int:
    del kind
    manifest_path = output / "run-manifest.json"
    sent: set[str] = set()
    while True:
        manifest = _read_json(manifest_path)
        if manifest:
            for stage, text in milestone_messages(manifest, sent):
                send_message(text)
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


def workflow_watchdog_text(*, scope: str, run_url: str) -> str:
    label = {"long": "الفيديو الطويل", "short": "الشورت", "bundle": "الطويل + الشورت"}.get(scope, "الإنتاج")
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
    watch_p.add_argument("--kind", choices=("long", "short"), required=True)
    watch_p.add_argument("--poll-seconds", type=float, default=4.0)

    final_p = sub.add_parser("terminal")
    final_p.add_argument("--output", type=Path, required=True)
    final_p.add_argument("--kind", choices=("long", "short"), required=True)
    final_p.add_argument("--job-status", required=True)
    final_p.add_argument("--run-url", default="")

    watchdog_p = sub.add_parser("watchdog")
    watchdog_p.add_argument("--output-root", type=Path, required=True)
    watchdog_p.add_argument("--scope", choices=("long", "short", "bundle"), required=True)
    watchdog_p.add_argument("--job-status", required=True)
    watchdog_p.add_argument("--run-url", default="")

    args = parser.parse_args()
    if args.command == "watch":
        return watch(args.output, kind=args.kind, poll_seconds=args.poll_seconds)
    if args.command == "watchdog":
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
