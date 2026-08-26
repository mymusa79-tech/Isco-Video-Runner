from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from scripts import telegram_operations_ui as ops_ui

_FAILURE_STAGES = (
    ("CHECKOUT_RUNNER_OUTCOME", "Checkout Runner"),
    ("CHECKOUT_ENGINE_OUTCOME", "Checkout Engine"),
    ("SETUP_PYTHON_OUTCOME", "Setup Python"),
    ("INSTALL_ENGINE_OUTCOME", "Install Engine"),
    ("RESTORE_STATE_OUTCOME", "Restore Memory"),
    ("VOICE_PREFLIGHT_OUTCOME", "Voice Preflight"),
    ("ENVIRONMENT_PREFLIGHT_OUTCOME", "Environment Preflight"),
    ("PREPARE_REQUEST_OUTCOME", "Prepare Request"),
    ("VERIFY_PROVIDERS_OUTCOME", "Provider Readiness"),
    ("PRODUCE_VIDEO_OUTCOME", "الإنتاج"),
    ("FINAL_REVIEW_OUTCOME", "Final Review"),
    ("UPLOAD_FINAL_OUTCOME", "Upload Final Bundle"),
    ("PUBLISH_APPROVAL_OUTCOME", "Publish Approval"),
    ("CREATE_RELEASE_OUTCOME", "Create Release"),
    ("PERSIST_STATE_OUTCOME", "Persist Accepted State"),
)

_EXCEPTION_PREFIX = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)|RuntimeError|ValueError|SystemExit):\s*"
)


def _telegram_request(token: str, method: str, payload: dict[str, str]) -> bool:
    if not token:
        return False
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(payload).encode("utf-8")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=35) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"Telegram {method} failed: {type(exc).__name__}: {exc}")
        return False
    if not body.get("ok"):
        print(f"Telegram {method} failed: {body.get('description', 'unknown API error')}")
        return False
    print(f"Telegram {method} succeeded")
    return True


def _read_json_optional(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def format_duration(seconds: int | float) -> str:
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes}د {secs}ث" if minutes else f"{secs}ث"


def detect_failure_stage(env: dict[str, str] | None = None) -> str:
    values = env if env is not None else os.environ
    for key, label in _FAILURE_STAGES:
        if str(values.get(key) or "") in {"failure", "cancelled"}:
            return label
    return "مرحلة غير محددة"


def compact_failure_reason(raw: str) -> str:
    """Remove exception-class noise without guessing a new root cause."""
    value = " ".join(str(raw or "").replace("\r", " ").replace("\n", " ").split())
    if not value:
        return "راجع تفاصيل GitHub لمعرفة السبب التقني."
    value = _EXCEPTION_PREFIX.sub("", value).strip()
    return value[:300] or "راجع تفاصيل GitHub لمعرفة السبب التقني."


def read_failure_reason(runner_temp: Path, stage: str) -> str:
    candidates: list[Path] = []
    if stage == "Final Review":
        candidates.append(runner_temp / "final-review-step.log")
    candidates.append(runner_temp / "production-step.log")
    pattern = re.compile(r"Traceback|Error|Exception|failed|blocked|RuntimeError|ValueError", re.IGNORECASE)
    for path in candidates:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        matching = [line.strip() for line in lines if pattern.search(line)]
        if matching:
            return compact_failure_reason(matching[-1])
    return "راجع تفاصيل GitHub لمعرفة السبب التقني."


def failure_impact(stage: str) -> str:
    if stage in {
        "Checkout Runner",
        "Checkout Engine",
        "Setup Python",
        "Install Engine",
        "Restore Memory",
        "Voice Preflight",
        "Environment Preflight",
        "Prepare Request",
        "Provider Readiness",
    }:
        return "توقفت المحاولة قبل اكتمال إنتاج الفيديو."
    if stage in {"الإنتاج", "Final Review", "Upload Final Bundle"}:
        return "لم تُعتمد حزمة نهائية لهذا التشغيل."
    if stage in {"Publish Approval", "Create Release", "Persist Accepted State"}:
        return "توقف مسار التسليم بعد الإنتاج؛ راجع GitHub قبل اعتبار الحزمة مكتملة."
    return "توقفت المحاولة ولم تُسجّل كنتيجة ناجحة."


def build_failure_message(*, run_number: str, elapsed_seconds: int, env: dict[str, str], runner_temp: Path) -> str:
    stage = detect_failure_stage(env)
    reason = read_failure_reason(runner_temp, stage)
    return ops_ui.render_failure_text(
        run_number=run_number,
        stage=stage,
        duration=format_duration(elapsed_seconds),
        reason=reason,
        impact=failure_impact(stage),
    )


def _bundle_summary(delivery: dict[str, Any], output_root: Path | None) -> str:
    if str(delivery.get("delivery_kind") or "") == "long_plus_shorts":
        try:
            short_count = int(delivery.get("short_count", 0) or 0)
        except (TypeError, ValueError):
            short_count = 0
        parts = ["الحلقة الطويلة"]
        if short_count > 0:
            parts.append(f"{short_count} Shorts")
        thumbnail_plan = _read_json_optional(output_root / "thumbnail-plan.json") if output_root else {}
        candidates = thumbnail_plan.get("candidates") if isinstance(thumbnail_plan, dict) else None
        if isinstance(candidates, list) and len(candidates) >= 3:
            parts.append("عناوين/صور A/B/C")
        return " + ".join(parts) + " جاهزة"
    return "الحزمة النهائية جاهزة"


def build_success_message(
    *,
    run_number: str,
    elapsed_seconds: int,
    plan_path: Path | None = None,
    delivery_path: Path | None = None,
    output_root: Path | None = None,
    request_path: Path | None = None,
) -> str:
    plan = _read_json_optional(plan_path)
    delivery = _read_json_optional(delivery_path)
    request = _read_json_optional(request_path)
    topic = str(request.get("topic") or plan.get("topic") or "").strip()
    plan_source = str(plan.get("plan_source") or "").strip()
    warning = ""
    if plan_source == "product_proof_fallback":
        warning = "استُخدم المحتوى الاحتياطي المعتمد (fallback) بدل تخطيط سحابي جديد لهذا الموضوع."
    return ops_ui.render_success_text(
        run_number=run_number,
        topic=topic,
        bundle_summary=_bundle_summary(delivery, output_root),
        duration=format_duration(elapsed_seconds),
        warning=warning,
        quality_passed=True,
    )


def deliver_terminal_message(*, token: str, chat_id: str, text: str, progress_message_id: str = "") -> bool:
    if progress_message_id:
        print(f"Telegram notify: editMessageText (message_id={progress_message_id})")
        return _telegram_request(
            token,
            "editMessageText",
            {"chat_id": chat_id, "message_id": progress_message_id, "text": text},
        )
    print("Telegram notify: sendMessage (no saved progress message_id)")
    return _telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": text})
