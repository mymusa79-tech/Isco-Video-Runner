from __future__ import annotations

import argparse
import io
import json
import os
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

RESUME_CACHE_NAMESPACE = "clean-v2-resume-v6"
RESUMABLE_STAGES = ("planning", "script", "text_audit", "voice", "visuals")
STAGE_LABELS = {
    "planning": "Planning",
    "script": "Script",
    "text_audit": "Text Audit",
    "voice": "Voice",
    "visuals": "Visuals",
}
RERUN_MAX_AGE_DAYS = 30

JsonGetter = Callable[[str], Any]
BytesGetter = Callable[[str], bytes]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _request(state: Mapping[str, Any], request_id: str, request_sha256: str) -> dict[str, Any]:
    requests = state.get("requests")
    if not isinstance(requests, dict):
        raise RuntimeError("Telegram state requests are missing")
    request = requests.get(request_id)
    if not isinstance(request, dict):
        raise RuntimeError("Telegram production request not found")
    if str(request.get("request_sha256") or "") != str(request_sha256 or ""):
        raise RuntimeError("Telegram production request hash mismatch")
    return request


def build_resume_cache_key(
    *, runner_os: str, runner_sha: str, engine_sha: str, request_sha256: str, run_id: str, run_attempt: str
) -> str:
    values = (runner_os, runner_sha, engine_sha, request_sha256, run_id, run_attempt)
    if any(not str(value or "").strip() for value in values):
        raise RuntimeError("resume cache identity is incomplete")
    return "-".join(
        (
            RESUME_CACHE_NAMESPACE,
            str(runner_os).strip(),
            str(runner_sha).strip(),
            str(engine_sha).strip(),
            str(request_sha256).strip(),
            str(run_id).strip(),
            str(run_attempt).strip(),
        )
    )


def record_production_start(
    state: dict[str, Any],
    *,
    request_id: str,
    request_sha256: str,
    run_id: str,
    run_attempt: str,
    run_url: str,
    runner_sha: str,
    engine_sha: str,
    resume_cache_key: str,
    at: str | None = None,
) -> dict[str, Any]:
    request = _request(state, request_id, request_sha256)
    existing = request.get("production")
    production = dict(existing) if isinstance(existing, dict) else {}
    previous_run_id = str(production.get("run_id") or "").strip()
    if previous_run_id and previous_run_id != str(run_id):
        raise RuntimeError("Telegram request cannot move to a different GitHub run")
    production.update(
        {
            "run_id": str(run_id),
            "run_attempt": int(run_attempt),
            "run_url": str(run_url),
            "runner_sha": str(runner_sha),
            "engine_sha": str(engine_sha),
            "resume_cache_key": str(resume_cache_key),
            "last_job_status": "in_progress",
            "manifest_status": "running",
            "artifact_id": None,
            "artifact_name": "",
            "artifact_url": "",
            "final_published": False,
            "started_at": str(at or utc_now()),
            "finished_at": None,
        }
    )
    request["production"] = production
    return request


def record_production_terminal(
    state: dict[str, Any],
    *,
    request_id: str,
    request_sha256: str,
    run_id: str,
    run_attempt: str,
    job_status: str,
    manifest_status: str,
    artifact_id: str = "",
    artifact_name: str = "",
    artifact_url: str = "",
    final_published: bool = False,
    at: str | None = None,
) -> dict[str, Any]:
    request = _request(state, request_id, request_sha256)
    production = request.get("production")
    if not isinstance(production, dict):
        production = {}
    previous_run_id = str(production.get("run_id") or "").strip()
    if previous_run_id and previous_run_id != str(run_id):
        raise RuntimeError("Telegram request terminal update changed GitHub run")
    production.update(
        {
            "run_id": str(run_id),
            "run_attempt": int(run_attempt),
            "last_job_status": str(job_status or "unknown"),
            "manifest_status": str(manifest_status or "unknown"),
            "artifact_id": int(artifact_id) if str(artifact_id or "").isdigit() else None,
            "artifact_name": str(artifact_name or ""),
            "artifact_url": str(artifact_url or ""),
            "final_published": bool(final_published),
            "finished_at": str(at or utc_now()),
        }
    )
    request["production"] = production
    return request


def incomplete_requests(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    requests = state.get("requests")
    if not isinstance(requests, dict):
        return []
    result = []
    for request in requests.values():
        if not isinstance(request, dict):
            continue
        production = request.get("production")
        if isinstance(production, dict) and production.get("final_published") is True:
            continue
        if not str(request.get("approved_topic") or "").strip():
            continue
        result.append(request)
    result.sort(
        key=lambda item: str(
            ((item.get("production") or {}).get("finished_at") if isinstance(item.get("production"), dict) else "")
            or ((item.get("production") or {}).get("started_at") if isinstance(item.get("production"), dict) else "")
            or item.get("selected_at")
            or ""
        ),
        reverse=True,
    )
    return result


def request_status_label(request: Mapping[str, Any]) -> str:
    production = request.get("production")
    if not isinstance(production, dict):
        status = str(request.get("status") or "غير مكتمل")
        return {
            "awaiting_confirmation": "بانتظار التأكيد",
            "confirmed_pending_dispatch": "بانتظار الإرسال",
            "dispatched": "أُرسل ولم تُسجّل نتيجة",
            "cancelled": "ملغى",
        }.get(status, status or "غير مكتمل")
    manifest_status = str(production.get("manifest_status") or "").strip()
    job_status = str(production.get("last_job_status") or "").strip()
    if manifest_status == "quality_pending":
        return "Quality Pending"
    if job_status == "failure" or manifest_status == "failed":
        return "فشل"
    if job_status == "cancelled" or manifest_status == "cancelled":
        return "ملغى"
    if job_status in {"in_progress", "queued", "rerun_requested"}:
        return "قيد التشغيل"
    if job_status == "success" and not production.get("final_published"):
        return "اكتمل الإنتاج ولم يُنشر نهائيًا"
    return manifest_status or job_status or "غير مكتمل"


def annotate_manifest(
    manifest_path: Path,
    checkpoint_path: Path,
    *,
    cache_key: str,
    save_allowed: bool,
    job_status: str = "",
) -> dict[str, Any] | None:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(manifest, dict):
        return None
    # A workflow timeout/cancellation can kill Python before the journal closes.
    # The always() step owns terminalizing that stale state, without discarding
    # the last certified resume checkpoint or changing a completed quality block.
    if manifest.get("status") == "running" and job_status in {"failure", "cancelled"}:
        finished = datetime.now(timezone.utc)
        finished_at = finished.isoformat()
        terminal_status = "cancelled" if job_status == "cancelled" else "failed"
        for stage in manifest.get("stages", []):
            if isinstance(stage, dict) and stage.get("status") == "running":
                stage.update({
                    "status": terminal_status,
                    "finished_at": finished_at,
                    "error_type": "WorkflowInterrupted",
                    "failure_classification": "infrastructure",
                })
                started_at = _parse_time(stage.get("started_at"))
                if started_at is not None:
                    stage["duration_seconds"] = round(
                        max(0.0, (finished - started_at).total_seconds()), 3
                    )
                manifest["failure_origin_stage"] = stage.get("name")
        manifest.update(
            status=terminal_status, finished_at=finished_at,
            failure_classification="infrastructure",
        )
    completed_stage = ""
    checkpoint_valid = False
    if save_allowed:
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            checkpoint = None
        if isinstance(checkpoint, dict):
            stage = str(checkpoint.get("completed_stage") or "")
            if stage in RESUMABLE_STAGES:
                completed_stage = stage
                checkpoint_valid = True
    manifest["durable_resume"] = {
        "schema_version": 1,
        "save_allowed": bool(save_allowed and checkpoint_valid),
        "cache_key": str(cache_key or ""),
        "completed_stage": completed_stage,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest["durable_resume"]


def completed_stage_from_manifest(manifest: Mapping[str, Any], *, expected_cache_key: str) -> str:
    durable = manifest.get("durable_resume")
    if not isinstance(durable, dict) or durable.get("schema_version") != 1:
        return ""
    if durable.get("save_allowed") is not True:
        return ""
    if str(durable.get("cache_key") or "") != str(expected_cache_key or ""):
        return ""
    stage = str(durable.get("completed_stage") or "")
    return stage if stage in RESUMABLE_STAGES else ""


def _manifest_from_artifact(data: bytes, request_id: str) -> dict[str, Any] | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [
                name
                for name in archive.namelist()
                if name.endswith("run-manifest.json") and request_id in name
            ]
            if not names:
                all_manifests = [name for name in archive.namelist() if name.endswith("run-manifest.json")]
                if len(all_manifests) == 1:
                    names = all_manifests
            if not names:
                return None
            names.sort(key=lambda value: (value.count("/"), len(value)))
            value = json.loads(archive.read(names[0]).decode("utf-8"))
            return value if isinstance(value, dict) else None
    except (OSError, ValueError, KeyError, UnicodeError, zipfile.BadZipFile):
        return None


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _disabled(reason: str, *, run_id: str = "", cache_key: str = "") -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "completed_stage": "",
        "stage_label": "",
        "run_id": str(run_id or ""),
        "cache_key": str(cache_key or ""),
    }


def evaluate_resume(
    request: Mapping[str, Any],
    *,
    current_runner_sha: str,
    current_engine_sha: str,
    github_json: JsonGetter,
    github_bytes: BytesGetter,
    now: datetime | None = None,
) -> dict[str, Any]:
    production = request.get("production")
    if not isinstance(production, dict):
        return _disabled("لا يوجد GitHub Run أصلي مسجّل لهذا الطلب.")
    if production.get("final_published") is True:
        return _disabled("هذا الطلب منشور نهائيًا ولا يحتاج استئنافًا.")

    run_id = str(production.get("run_id") or "").strip()
    cache_key = str(production.get("resume_cache_key") or "").strip()
    if not run_id:
        return _disabled("لا يوجد GitHub Run أصلي مسجّل لهذا الطلب.")
    original_runner = str(production.get("runner_sha") or "").strip()
    original_engine = str(production.get("engine_sha") or "").strip()
    if not current_runner_sha:
        return _disabled("تعذر تحديد SHA الحالي لـmain بأمان.", run_id=run_id)
    if original_runner != current_runner_sha:
        return _disabled("تغيّر Runner SHA منذ المحاولة الأصلية؛ يلزم بدء من جديد.", run_id=run_id)
    if not current_engine_sha or original_engine != current_engine_sha:
        return _disabled("تغيّر Engine SHA منذ المحاولة الأصلية؛ يلزم بدء من جديد.", run_id=run_id)
    if not cache_key:
        return _disabled("المحاولة الأصلية لا تحتوي مفتاح checkpoint مسجّلًا.", run_id=run_id)

    try:
        run = github_json(f"actions/runs/{run_id}")
    except Exception:
        return _disabled("تعذر التحقق من GitHub Run الأصلي عبر API.", run_id=run_id, cache_key=cache_key)
    if not isinstance(run, dict):
        return _disabled("GitHub Run الأصلي غير متاح.", run_id=run_id, cache_key=cache_key)
    if str(run.get("head_sha") or "") != current_runner_sha:
        return _disabled("GitHub Run الأصلي لا يطابق SHA الحالي لـmain.", run_id=run_id, cache_key=cache_key)
    if str(run.get("status") or "") != "completed":
        return _disabled("يوجد تشغيل لنفس الطلب ما زال قائمًا؛ لا يمكن بدء استئناف موازٍ.", run_id=run_id, cache_key=cache_key)
    created_at = _parse_time(run.get("created_at"))
    current_time = now or datetime.now(timezone.utc)
    if created_at and current_time - created_at > timedelta(days=RERUN_MAX_AGE_DAYS):
        return _disabled("تجاوز GitHub Run مدة السماح بإعادة التشغيل (30 يومًا).", run_id=run_id, cache_key=cache_key)

    query = urllib.parse.urlencode({"key": cache_key, "ref": "refs/heads/main", "per_page": 100})
    try:
        cache_payload = github_json(f"actions/caches?{query}")
    except Exception:
        return _disabled("تعذر التحقق من Actions cache عبر GitHub API.", run_id=run_id, cache_key=cache_key)
    caches = cache_payload.get("actions_caches") if isinstance(cache_payload, dict) else None
    exact_cache = next(
        (
            item
            for item in (caches if isinstance(caches, list) else [])
            if isinstance(item, dict)
            and str(item.get("key") or "") == cache_key
            and str(item.get("ref") or "") == "refs/heads/main"
        ),
        None,
    )
    if exact_cache is None:
        return _disabled("checkpoint غير موجود في Actions cache؛ انتهت صلاحيته أو لم يُحفظ.", run_id=run_id, cache_key=cache_key)

    artifact_id = production.get("artifact_id")
    artifact: dict[str, Any] | None = None
    if isinstance(artifact_id, int) and artifact_id > 0:
        try:
            candidate = github_json(f"actions/artifacts/{artifact_id}")
            artifact = candidate if isinstance(candidate, dict) else None
        except Exception:
            artifact = None
    if artifact is None:
        try:
            listed = github_json(f"actions/runs/{run_id}/artifacts?per_page=100")
        except Exception:
            listed = None
        rows = listed.get("artifacts") if isinstance(listed, dict) else None
        choices = [
            item
            for item in (rows if isinstance(rows, list) else [])
            if isinstance(item, dict)
            and not item.get("expired")
            and str(item.get("name") or "").startswith("clean-v2-telegram-")
        ]
        choices.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        artifact = choices[0] if choices else None
    if not isinstance(artifact, dict) or artifact.get("expired") is True:
        return _disabled("artifact الدليل غير متاح أو انتهت صلاحيته؛ لا يمكن إثبات مرحلة الاستئناف.", run_id=run_id, cache_key=cache_key)
    resolved_artifact_id = str(artifact.get("id") or "").strip()
    if not resolved_artifact_id:
        return _disabled("artifact الدليل لا يحتوي معرّفًا صالحًا.", run_id=run_id, cache_key=cache_key)
    try:
        archive = github_bytes(f"actions/artifacts/{resolved_artifact_id}/zip")
    except Exception:
        return _disabled("تعذر تنزيل artifact الدليل للتحقق من run-manifest.json.", run_id=run_id, cache_key=cache_key)
    manifest = _manifest_from_artifact(archive, str(request.get("request_id") or ""))
    if manifest is None:
        return _disabled("artifact لا يحتوي run-manifest.json صالحًا لهذا الطلب.", run_id=run_id, cache_key=cache_key)
    if str(manifest.get("runner_sha") or "") != current_runner_sha:
        return _disabled("run-manifest.json لا يطابق Runner SHA الحالي.", run_id=run_id, cache_key=cache_key)
    if str(manifest.get("engine_sha") or "") != current_engine_sha:
        return _disabled("run-manifest.json لا يطابق Engine SHA الحالي.", run_id=run_id, cache_key=cache_key)
    stage = completed_stage_from_manifest(manifest, expected_cache_key=cache_key)
    if not stage:
        return _disabled("run-manifest.json لا يثبت checkpoint دائمًا صالحًا لهذه المحاولة.", run_id=run_id, cache_key=cache_key)
    return {
        "available": True,
        "reason": "",
        "completed_stage": stage,
        "stage_label": STAGE_LABELS[stage],
        "run_id": run_id,
        "cache_key": cache_key,
        "artifact_id": resolved_artifact_id,
    }


def _github_request(path: str, *, method: str = "GET", want_bytes: bool = False) -> Any:
    token = str(os.environ.get("GITHUB_RUNTIME_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    repository = str(os.environ.get("GITHUB_REPOSITORY") or "").strip()
    api = str(os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    if not token or not repository:
        raise RuntimeError("GitHub runtime credentials are unavailable")
    request = urllib.request.Request(
        f"{api}/repos/{repository}/{path.lstrip('/')}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "isco-clean-v2-telegram-resume-history",
        },
        method=method,
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read()
    if want_bytes:
        return raw
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def github_json(path: str) -> Any:
    return _github_request(path)


def github_bytes(path: str) -> bytes:
    value = _github_request(path, want_bytes=True)
    return value if isinstance(value, bytes) else bytes(value)


def rerun_workflow(run_id: str) -> None:
    _github_request(f"actions/runs/{str(run_id).strip()}/rerun", method="POST")


def _main() -> int:
    parser = argparse.ArgumentParser(description="Telegram Clean V2 resume/history helpers")
    sub = parser.add_subparsers(dest="command", required=True)

    annotate = sub.add_parser("annotate-manifest")
    annotate.add_argument("--manifest", type=Path, required=True)
    annotate.add_argument("--checkpoint", type=Path, required=True)
    annotate.add_argument("--cache-key", required=True)
    annotate.add_argument("--save-allowed", choices=("true", "false"), required=True)
    annotate.add_argument("--job-status", choices=("success", "failure", "cancelled", "skipped"), default="success")

    args = parser.parse_args()
    if args.command == "annotate-manifest":
        result = annotate_manifest(
            args.manifest,
            args.checkpoint,
            cache_key=args.cache_key,
            save_allowed=args.save_allowed == "true",
            job_status=args.job_status,
        )
        print(json.dumps(result or {}, ensure_ascii=False, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
