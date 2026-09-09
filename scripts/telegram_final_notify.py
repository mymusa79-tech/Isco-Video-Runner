from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from scripts import telegram_operations_ui as ops_ui
from scripts.qc_pending_resume_bundle_v1 import validate_resume_bundle

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
_QC_PENDING_BUNDLE_DIRNAME = "short-circuit-gold-resume-v2"


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


def _path_optional(value: str) -> Path | None:
    text = str(value or "").strip()
    return Path(text) if text else None


def _current_qc_pending() -> dict[str, Any] | None:
    """Return only a complete current-run Gold recovery contract.

    A local checkpoint alone is not enough to advertise a button.  The exact-byte
    bundle must also validate, so Telegram never offers a resume action that would
    require replanning, rerendering, or weakening Gold.
    """
    roots = sorted(
        (path for path in Path("engine/output").glob("*") if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for root in roots:
        checkpoint_path = root / "qc-pending.json"
        if not checkpoint_path.is_file():
            continue
        checkpoint = _read_json_optional(checkpoint_path)
        if (
            checkpoint.get("contract_id") != "gold.qc-pending.v1"
            or checkpoint.get("schema_version") != 2
            or checkpoint.get("status") != "GOLD_VISION_PENDING_PROVIDER_CAPACITY"
            or checkpoint.get("release_allowed") is not False
            or checkpoint.get("resumable") is not True
            or checkpoint.get("failure_taxonomy") != "VisionProviderMeshUnavailableError"
        ):
            return None
        source_run_id = str(checkpoint.get("source_run_id") or "").strip()
        source_attempt = str(checkpoint.get("source_run_attempt") or "").strip()
        runner_sha = str(checkpoint.get("runner_sha") or "").strip().lower()
        engine_sha = str(checkpoint.get("engine_sha") or "").strip().lower()
        if source_run_id != str(os.environ.get("GITHUB_RUN_ID") or "").strip():
            return None
        if source_attempt != str(os.environ.get("GITHUB_RUN_ATTEMPT") or "").strip():
            return None
        if runner_sha != str(os.environ.get("GITHUB_SHA") or "").strip().lower():
            return None
        bundle = root / _QC_PENDING_BUNDLE_DIRNAME
        try:
            manifest = validate_resume_bundle(
                bundle,
                expected_source_run_id=source_run_id,
                expected_runner_sha=runner_sha,
                expected_engine_sha=engine_sha,
            )
        except Exception:
            return None
        if str(manifest.get("final_sha256") or "") != str((checkpoint.get("final") or {}).get("sha256") or ""):
            return None
        return checkpoint
    return None


def _approved_request_id(runner_temp: Path) -> str:
    request = _read_json_optional(runner_temp / "isco-control" / "approved-request.json")
    value = str(request.get("request_id") or "").strip()
    if not value or len(value) > 48 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in value):
        return ""
    return value


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


def terminal_delivery_status(env: dict[str, str] | None = None) -> str:
    """Classify the user-visible terminal state by the durable delivery boundary."""
    values = env if env is not None else os.environ
    job_status = str(values.get("JOB_STATUS") or "failure").strip().lower()
    release_outcome = str(values.get("CREATE_RELEASE_OUTCOME") or "").strip().lower()
    if release_outcome == "success":
        return "success" if job_status == "success" else "released_degraded"
    return "success" if job_status == "success" else "failure"


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
        "Checkout Runner", "Checkout Engine", "Setup Python", "Install Engine",
        "Restore Memory", "Voice Preflight", "Environment Preflight", "Prepare Request",
        "Provider Readiness",
    }:
        return "توقفت المحاولة قبل اكتمال إنتاج الفيديو."
    if stage in {"الإنتاج", "Final Review", "Upload Final Bundle"}:
        return "لم تُعتمد حزمة نهائية لهذا التشغيل."
    if stage in {"Publish Approval", "Create Release"}:
        return "توقف مسار التسليم بعد الإنتاج؛ راجع GitHub قبل اعتبار الحزمة مكتملة."
    if stage == "Persist Accepted State":
        return "تم إنشاء الإصدار، لكن حفظ الذاكرة عبر التشغيلات لم يكتمل. لا تعِد الإنتاج."
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


def build_qc_pending_message(*, run_number: str, elapsed_seconds: int, checkpoint: dict[str, Any]) -> str:
    final_sha = str((checkpoint.get("final") or {}).get("sha256") or "").strip()
    return (
        f"🟠 Final Master محفوظ — Gold بانتظار سعة Vision\n\n"
        f"التشغيل: #{run_number}\n"
        f"المدة: {format_duration(elapsed_seconds)}\n\n"
        "✅ Final Master: PASS\n"
        "✅ نفس final.mp4 محفوظ بدون إعادة رندر\n"
        "⏸️ Gold Vision: توقف مؤقت بسبب عدم توفر مزودي الرؤية\n"
        "🔒 النشر ما زال محجوبًا حتى ينجح Gold\n\n"
        "اضغط «▶️ تابع Gold» لاستكمال Gold فقط؛ لن يُعاد التخطيط أو البحث أو الصوت أو الرندر."
        + (f"\n\nSHA: {final_sha[:12]}" if final_sha else "")
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
    additional_warning: str = "",
) -> str:
    plan = _read_json_optional(plan_path)
    delivery = _read_json_optional(delivery_path)
    request = _read_json_optional(request_path)
    topic = str(request.get("topic") or plan.get("topic") or "").strip()
    plan_source = str(plan.get("plan_source") or "").strip()
    warnings: list[str] = []
    if plan_source == "product_proof_fallback":
        warnings.append("استُخدم المحتوى الاحتياطي المعتمد (fallback) بدل تخطيط سحابي جديد لهذا الموضوع.")
    if str(additional_warning or "").strip():
        warnings.append(str(additional_warning).strip())
    return ops_ui.render_success_text(
        run_number=run_number,
        topic=topic,
        bundle_summary=_bundle_summary(delivery, output_root),
        duration=format_duration(elapsed_seconds),
        warning=" ".join(warnings),
        quality_passed=True,
    )


def released_degraded_warning(env: dict[str, str]) -> str:
    stage = detect_failure_stage(env)
    if stage == "Persist Accepted State":
        return (
            "تم إنشاء GitHub Release بنجاح، لكن حفظ الذاكرة المقبولة عبر التشغيلات فشل. "
            "لا تعِد الإنتاج؛ راجع GitHub لإصلاح حفظ الحالة فقط."
        )
    return (
        "تم إنشاء GitHub Release بنجاح، لكن خطوة لاحقة فشلت. "
        "لا تعِد الإنتاج؛ راجع GitHub لمعالجة الحالة اللاحقة فقط."
    )


def terminal_keyboard(
    *,
    job_status: str,
    run_url: str,
    results_url: str = "",
    run_id: str = "",
    progress_message_id: str = "",
    request_id: str = "",
) -> dict[str, list[list[dict[str, str]]]]:
    rows: list[list[dict[str, str]]] = []
    run_value = str(run_url or "").strip()
    results_value = str(results_url or "").strip()
    run_id_value = str(run_id or "").strip()
    message_id_value = str(progress_message_id or "").strip()
    request_id_value = str(request_id or "").strip()
    release_ready = job_status in {"success", "released_degraded"}
    if job_status == "qc_pending" and request_id_value:
        callback = f"cmd:goldresume-{request_id_value}"
        if len(callback.encode("utf-8")) <= 64:
            rows.append([ops_ui.callback_button("▶️ تابع Gold", callback)])
    if run_id_value and message_id_value:
        try:
            details_data = ops_ui.operations_callback_data(ops_ui.ACTION_DETAILS, run_id_value, message_id_value)
        except ValueError:
            details_data = ""
        if details_data:
            rows.append([ops_ui.callback_button("📋 التفاصيل", details_data)])
    if release_ready and results_value:
        rows.append([ops_ui.url_button("📦 عرض النتائج", results_value)])
    if run_value:
        label = "🔗 GitHub" if release_ready else "📋 GitHub Logs"
        rows.append([ops_ui.url_button(label, run_value)])
    return ops_ui.inline_keyboard(rows)


def terminal_url_keyboard(*, job_status: str, run_url: str, results_url: str = "") -> dict[str, list[list[dict[str, str]]]]:
    """Compatibility helper retained for URL-only callers and tests."""
    return terminal_keyboard(job_status=job_status, run_url=run_url, results_url=results_url)


def deliver_terminal_message(
    *, token: str, chat_id: str, text: str, progress_message_id: str = "",
    reply_markup: dict[str, Any] | None = None,
) -> bool:
    base_payload: dict[str, str] = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        base_payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False, separators=(",", ":"))
    if progress_message_id:
        edit_payload = dict(base_payload)
        edit_payload["message_id"] = progress_message_id
        print(f"Telegram notify: editMessageText (message_id={progress_message_id})")
        if _telegram_request(token, "editMessageText", edit_payload):
            print("TELEGRAM_TERMINAL_DELIVERY=edited")
            return True
        print("Telegram notify: terminal edit failed; bounded sendMessage fallback")
        if _telegram_request(token, "sendMessage", base_payload):
            print("TELEGRAM_TERMINAL_DELIVERY=fallback_sent")
            return True
        print("TELEGRAM_TERMINAL_DELIVERY=failed")
        return False
    print("Telegram notify: sendMessage (no saved progress message_id)")
    delivered = _telegram_request(token, "sendMessage", base_payload)
    print(f"TELEGRAM_TERMINAL_DELIVERY={'sent' if delivered else 'failed'}")
    return delivered


def _elapsed_seconds(env: dict[str, str]) -> int:
    try:
        start = int(str(env.get("JOB_START_EPOCH") or "0"))
    except ValueError:
        start = 0
    if start <= 0:
        return 0
    import time
    return max(0, int(time.time()) - start)


def _run_url(env: dict[str, str]) -> str:
    explicit = str(env.get("RUN_URL") or "").strip()
    if explicit:
        return explicit
    server = str(env.get("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
    repository = str(env.get("GITHUB_REPOSITORY") or "").strip()
    run_id = str(env.get("GITHUB_RUN_ID") or "").strip()
    if repository and run_id:
        return f"{server}/{repository}/actions/runs/{run_id}"
    return ""


def _results_url(env: dict[str, str]) -> str:
    if str(env.get("CREATE_RELEASE_OUTCOME") or "") != "success":
        return ""
    server = str(env.get("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
    repository = str(env.get("GITHUB_REPOSITORY") or "").strip()
    run_number = str(env.get("GITHUB_RUN_NUMBER") or "").strip()
    release_tag = str(env.get("ISCO_RELEASE_TAG_OVERRIDE") or "").strip()
    if not release_tag and run_number:
        release_tag = f"video-{run_number}"
    if repository and release_tag:
        return f"{server}/{repository}/releases/tag/{release_tag}"
    return ""


def main() -> int:
    env = dict(os.environ)
    token = str(env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = str(env.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        print("Telegram terminal notify disabled: bot token or chat id is missing")
        return 0

    run_number = str(env.get("GITHUB_RUN_NUMBER") or "").strip()
    runner_temp = Path(str(env.get("RUNNER_TEMP") or "."))
    elapsed = _elapsed_seconds(env)
    checkpoint = _current_qc_pending()
    terminal_status = "qc_pending" if checkpoint is not None else terminal_delivery_status(env)
    request_id = _approved_request_id(runner_temp) if terminal_status == "qc_pending" else ""

    if terminal_status == "qc_pending":
        text = build_qc_pending_message(
            run_number=run_number,
            elapsed_seconds=elapsed,
            checkpoint=checkpoint or {},
        )
    elif terminal_status in {"success", "released_degraded"}:
        text = build_success_message(
            run_number=run_number,
            elapsed_seconds=elapsed,
            plan_path=_path_optional(str(env.get("FINAL_PLAN_PATH") or "")),
            delivery_path=_path_optional(str(env.get("FINAL_DELIVERY_PATH") or "")),
            output_root=_path_optional(str(env.get("FINAL_OUTPUT_ROOT") or "")),
            request_path=runner_temp / "isco-request.json",
            additional_warning=released_degraded_warning(env) if terminal_status == "released_degraded" else "",
        )
    else:
        text = build_failure_message(
            run_number=run_number,
            elapsed_seconds=elapsed,
            env=env,
            runner_temp=runner_temp,
        )

    progress_message_id = ""
    progress_id_path = runner_temp / "telegram-progress-message-id.txt"
    if progress_id_path.is_file():
        try:
            progress_message_id = progress_id_path.read_text(encoding="utf-8").strip()
        except OSError:
            progress_message_id = ""

    keyboard = terminal_keyboard(
        job_status=terminal_status,
        run_url=_run_url(env),
        results_url=_results_url(env),
        run_id=str(env.get("GITHUB_RUN_ID") or "").strip(),
        progress_message_id=progress_message_id,
        request_id=request_id,
    )
    delivered = deliver_terminal_message(
        token=token,
        chat_id=chat_id,
        text=text,
        progress_message_id=progress_message_id,
        reply_markup=keyboard,
    )
    return 0 if delivered else 1


if __name__ == "__main__":
    raise SystemExit(main())
