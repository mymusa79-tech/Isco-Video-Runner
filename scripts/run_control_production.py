from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from isco_video_agent.short_planner import DEFAULT_SIBLING_SPACING_HOURS, select_sibling_jobs

import scripts.planning_runtime_contract as planning_runtime_contract
import scripts.run_v3_voice as production
from scripts.control_approved_brief import materialize_approved_brief
from scripts.immutable_planning_snapshot import snapshot_approved_brief
from scripts.native_short_planner_router import install_native_short_router
from scripts.orchestration_shorts_port import finalize_short_quality, prepare_authoritative_short_for_gold
from scripts.short_finishing_capabilities import (
    ShortFinishingCapabilities,
    bind_short_finishing_capabilities,
)
from scripts.sibling_short_orchestration import orchestrate_sibling_shorts, stage_sibling_assets
from scripts.source_derived_short_planner import install_source_derived_short_planner
from scripts.unified_delivery import write_delivery_manifest

CONTROL_CHILD_TIMEOUT_SECONDS = 1200
ISOLATED_CHILD_CHECKPOINT_MODE = "isolated_sibling_child_no_cross_run"


def _canonical_request_hash(request: dict[str, Any]) -> str:
    subject = {key: value for key, value in request.items() if key != "request_sha256"}
    encoded = json.dumps(subject, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_control_request(request: dict[str, Any], expected_sha256: str) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise RuntimeError("Control request must be an object")
    stored = str(request.get("request_sha256") or "")
    current = _canonical_request_hash(request)
    if not stored or stored != current or stored != str(expected_sha256 or ""):
        raise RuntimeError("Control request changed after Telegram approval")
    if request.get("approved_by_user") is not True:
        raise RuntimeError("Control request lacks explicit user approval")
    if request.get("source") != "telegram_editorial_control_panel":
        raise RuntimeError("Unsupported control request source")
    if request.get("status") != "approved_waiting_production_activation":
        raise RuntimeError("Control request is not in an approved production-waiting state")
    if request.get("kind") not in {"long", "short"}:
        raise RuntimeError("Control request kind is unsupported")
    if request.get("production_dispatch_authorized") is not False:
        raise RuntimeError("Stored control request must remain non-dispatching")
    if request.get("kind") == "short" and request.get("approval_scope") not in {"short_only", "short_sibling"}:
        raise RuntimeError("Short control request has an unsupported approval scope")
    return request


def load_control_request(path: Path, expected_sha256: str) -> dict[str, Any]:
    try:
        request = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("Control request is missing or invalid JSON") from exc
    return validate_control_request(request, expected_sha256)


def _output_dirs() -> set[Path]:
    return {path.resolve() for path in Path("output").glob("*") if path.is_dir()}


def _new_output_dir(before: set[Path]) -> Path:
    after = _output_dirs()
    created = [path for path in after if path not in before]
    if len(created) != 1:
        raise RuntimeError(f"Control production expected exactly one new output directory, found {len(created)}")
    return created[0]


def write_sibling_short_plan(output_dir: Path, request: dict[str, Any]) -> Path | None:
    if request.get("approval_scope") != "long_plus_sibling_shorts":
        return None
    plan_path = Path(output_dir) / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    sections = plan.get("sections") if isinstance(plan, dict) else None
    if not isinstance(sections, list):
        raise RuntimeError("Long plan cannot produce sibling Short semantic jobs")
    jobs = select_sibling_jobs(
        str(item.get("key_point") or "").strip()
        for item in sections
        if isinstance(item, dict) and str(item.get("key_point") or "").strip()
    )
    minimum = int((request.get("sibling_shorts") or {}).get("minimum", 2))
    maximum = int((request.get("sibling_shorts") or {}).get("maximum", 3))
    if minimum < 2 or maximum > 3 or minimum > maximum:
        raise RuntimeError("Sibling Short quota must stay within 2–3")
    if len(jobs) < minimum:
        raise RuntimeError("Long episode does not contain enough independent jobs for approved sibling Shorts")
    selected = list(jobs[:maximum])
    document = {
        "schema_version": 1,
        "source_request_id": request.get("request_id"),
        "source_request_sha256": request.get("request_sha256"),
        "source_production_plan_sha256": _sha256_file(plan_path),
        "source_topic": request.get("approved_topic"),
        "short_count": len(selected),
        "semantic_jobs": [
            {
                "index": index,
                "semantic_job": job,
                "status": "planned_not_dispatched",
                "production_dispatch_authorized": False,
            }
            for index, job in enumerate(selected, 1)
        ],
        "publish_spacing_hours": DEFAULT_SIBLING_SPACING_HOURS,
        "automatic_production_started": False,
        "youtube_publish_mode": "manual_in_youtube_studio",
    }
    path = Path(output_dir) / "sibling-short-plan.json"
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _set_runtime_env(values: dict[str, str]) -> dict[str, str | None]:
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    return previous


def _restore_runtime_env(previous: dict[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _short_router_for_request(request: dict[str, Any]):
    scope = request.get("approval_scope")
    if scope == "short_sibling":
        return (lambda: install_source_derived_short_planner(request)), "source_derived_long_episode_short"
    if scope == "short_only":
        return install_native_short_router, "native_short_resilient_mesh"
    raise RuntimeError("Unsupported Short approval scope")


def _isolated_child_requested(request: dict[str, Any]) -> bool:
    flag = (os.environ.get("ISCO_RUNTIME_ISOLATED_CHILD") or "").strip().lower()
    if flag not in {"1", "true", "yes", "on"}:
        return False
    if request.get("kind") != "short" or request.get("approval_scope") != "short_sibling":
        raise RuntimeError("isolated child runtime is restricted to approved sibling Shorts")
    parent_id = str(request.get("parent_control_request_id") or "").strip()
    if not parent_id:
        raise RuntimeError("isolated sibling child runtime requires parent request identity")
    mode = (os.environ.get("ISCO_RUNTIME_CHECKPOINT_MODE") or "").strip()
    if mode != ISOLATED_CHILD_CHECKPOINT_MODE:
        raise RuntimeError("isolated sibling child runtime checkpoint mode is invalid")
    return True


def _materialize_isolated_child_snapshot(brief_path: Path, runtime_dir: Path) -> tuple[Path, str]:
    raw_sha = _sha256_file(brief_path)
    snapshot = snapshot_approved_brief(
        brief_path,
        Path(runtime_dir) / "approved-brief.snapshot.json",
        expected_sha256=raw_sha,
    )
    return snapshot, raw_sha


def _runtime_env_for_request(
    request: dict[str, Any],
    *,
    brief_path: Path,
    brief_hash: str,
    request_file: Path,
    runtime_dir: Path,
) -> dict[str, str]:
    values = {
        "ISCO_APPROVED_BRIEF_PATH": str(brief_path.resolve()),
        "ISCO_APPROVED_BRIEF_SHA256": brief_hash,
        "REQUEST_FILE": str(request_file.resolve()),
        "ISCO_CONTROL_REQUEST_ID": str(request.get("request_id") or ""),
    }
    parent_id = str(request.get("parent_control_request_id") or "").strip()
    if parent_id:
        values["ISCO_CONTROL_PARENT_REQUEST_ID"] = parent_id
    if _isolated_child_requested(request):
        snapshot, raw_sha = _materialize_isolated_child_snapshot(brief_path, runtime_dir)
        values.update(
            {
                "ISCO_APPROVED_BRIEF_SNAPSHOT_PATH": str(snapshot.resolve()),
                "ISCO_APPROVED_BRIEF_SNAPSHOT_SHA256": raw_sha,
                "ISCO_RUNTIME_CHECKPOINT_MODE": ISOLATED_CHILD_CHECKPOINT_MODE,
            }
        )
    return values


def execute_control_request(request: dict[str, Any], *, runtime_dir: Path) -> Path:
    validate_control_request(request, str(request.get("request_sha256") or ""))
    runtime_dir = Path(runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    request_copy = runtime_dir / "control-request.json"
    request_copy.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    brief_path, brief_hash = materialize_approved_brief(request, runtime_dir / "approved-brief.json")
    request_file = runtime_dir / "isco-request.json"
    request_file.write_text(
        json.dumps(
            {
                "topic": request["approved_topic"],
                "format": "moment" if request["kind"] == "short" else request["format"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    previous_env = _set_runtime_env(
        _runtime_env_for_request(
            request,
            brief_path=brief_path,
            brief_hash=brief_hash,
            request_file=request_file,
            runtime_dir=runtime_dir,
        )
    )
    runtime_request = dict(request)
    runtime_request["production_dispatch_authorized"] = True
    original_gold = production.run_gold_enforce_phase4
    # production.main() no longer calls a bare install_router() itself - since the
    # planning seam consolidation it calls install_entrypoint_planning_contracts(),
    # which resolves install_router from scripts.planning_runtime_contract's own module
    # globals. Patching scripts.run_v3_voice.install_router (removed by that same
    # consolidation) is a dead no-op that used to crash outright; patch the name that is
    # actually looked up at call time instead.
    original_install_router = planning_runtime_contract.install_router
    original_resolve_plan_source = production._resolve_plan_source
    original_budget_factory = production._production_budget_ledger
    short_pre: dict[str, Any] | None = None
    ledger_box: dict[str, Any] = {}
    before = _output_dirs()

    def captured_budget_factory(fmt: str):
        ledger = original_budget_factory(fmt)
        ledger_box["ledger"] = ledger
        return ledger

    def controlled_gold(**kwargs):
        nonlocal short_pre
        if request["kind"] == "short":
            output_dir = Path(kwargs["output_dir"])
            ledger = ledger_box.get("ledger")
            if ledger is None:
                raise RuntimeError("Short V2 lost the production AI budget ledger before voice synthesis")
            capabilities = ShortFinishingCapabilities.from_gold_kwargs(kwargs)
            with bind_short_finishing_capabilities(capabilities):
                short_pre = prepare_authoritative_short_for_gold(
                    output_dir,
                    runtime_request,
                    ledger=ledger,
                    run_final_master_qc=production.run_final_master_qc,
                )
        result = original_gold(**kwargs)
        if request["kind"] == "short":
            assert short_pre is not None
            finalize_short_quality(Path(kwargs["output_dir"]), runtime_request, short_pre)
        return result

    production.run_gold_enforce_phase4 = controlled_gold
    production._production_budget_ledger = captured_budget_factory
    if request["kind"] == "short":
        short_install, plan_source = _short_router_for_request(request)
        planning_runtime_contract.install_router = short_install
        production._resolve_plan_source = lambda: plan_source
    try:
        production.main()
        output = _new_output_dir(before)
    finally:
        production.run_gold_enforce_phase4 = original_gold
        planning_runtime_contract.install_router = original_install_router
        production._resolve_plan_source = original_resolve_plan_source
        production._production_budget_ledger = original_budget_factory
        _restore_runtime_env(previous_env)

    if request["kind"] == "long":
        write_sibling_short_plan(output, request)
    return output


def _isolated_child_process_env(child_request: dict[str, Any], request_path: Path) -> dict[str, str]:
    parent_id = str(child_request.get("parent_control_request_id") or "").strip()
    if child_request.get("kind") != "short" or child_request.get("approval_scope") != "short_sibling" or not parent_id:
        raise RuntimeError("child subprocess isolation requires an approved sibling Short request")
    env = os.environ.copy()
    env["CONTROL_PLANE_PRODUCTION_ENABLED"] = "true"
    env["ISCO_CONTROL_REQUEST_PATH"] = str(request_path.resolve())
    env["ISCO_CONTROL_REQUEST_SHA256"] = str(child_request.get("request_sha256") or "")
    env["ISCO_RUNTIME_ISOLATED_CHILD"] = "1"
    env["ISCO_RUNTIME_CHECKPOINT_MODE"] = ISOLATED_CHILD_CHECKPOINT_MODE
    env["ISCO_CONTROL_PARENT_REQUEST_ID"] = parent_id
    # P0 state isolation: never let a child inherit the parent's immutable approved
    # input binding. The child materializes a fresh read-only snapshot from its exact
    # inherited approval before production.main() installs the canonical runtime seam.
    for key in (
        "ISCO_APPROVED_BRIEF_PATH",
        "ISCO_APPROVED_BRIEF_SHA256",
        "ISCO_APPROVED_BRIEF_SNAPSHOT_PATH",
        "ISCO_APPROVED_BRIEF_SNAPSHOT_SHA256",
        "REQUEST_FILE",
        "ISCO_CONTROL_REQUEST_ID",
    ):
        env.pop(key, None)
    return env


def execute_child_subprocess(child_request: dict[str, Any], *, runtime_root: Path) -> Path:
    validate_control_request(child_request, str(child_request.get("request_sha256") or ""))
    child_dir = Path(runtime_root) / str(child_request.get("request_id") or "child")
    child_dir.mkdir(parents=True, exist_ok=True)
    request_path = child_dir / "child-control-request.json"
    request_path.write_text(json.dumps(child_request, ensure_ascii=False, indent=2), encoding="utf-8")
    env = _isolated_child_process_env(child_request, request_path)
    before = _output_dirs()
    try:
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve())],
            check=True,
            env=env,
            timeout=CONTROL_CHILD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Sibling Short production timed out after {CONTROL_CHILD_TIMEOUT_SECONDS}s"
        ) from exc
    return _new_output_dir(before)


def execute_bundle(parent_request: dict[str, Any], *, runtime_root: Path) -> Path:
    parent_output = execute_control_request(parent_request, runtime_dir=Path(runtime_root) / "parent")
    if parent_request.get("approval_scope") != "long_plus_sibling_shorts":
        return parent_output

    sibling_plan = parent_output / "sibling-short-plan.json"
    if not sibling_plan.is_file():
        raise RuntimeError("Approved long+Shorts request completed without sibling-short-plan.json")

    def execute_child(child_request: dict[str, Any]) -> Path:
        return execute_child_subprocess(child_request, runtime_root=runtime_root)

    completed = orchestrate_sibling_shorts(parent_request, sibling_plan, execute_short=execute_child)
    staged = stage_sibling_assets(parent_output, completed)
    (parent_output / "sibling-short-results.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "parent_request_id": parent_request.get("request_id"),
                "parent_request_sha256": parent_request.get("request_sha256"),
                "short_count": len(staged),
                "shorts": staged,
                "execution_mode": "sequential_isolated_subprocesses",
                "short_source_mode": "exact_long_episode_sections",
                "partial_delivery_allowed": False,
                "youtube_publish_mode": "manual_in_youtube_studio",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    write_delivery_manifest(
        parent_output,
        repository=(os.environ.get("GITHUB_REPOSITORY") or "mymusa79-tech/Isco-Video-Runner").strip(),
        release_tag=None,
        request=parent_request,
        short_assets=staged,
    )
    return parent_output


def main() -> None:
    if (os.environ.get("CONTROL_PLANE_PRODUCTION_ENABLED") or "false").strip().lower() != "true":
        raise RuntimeError("Control-plane production is locked; no Production Run is authorized")
    request_path = Path(os.environ.get("ISCO_CONTROL_REQUEST_PATH") or "")
    expected = (os.environ.get("ISCO_CONTROL_REQUEST_SHA256") or "").strip()
    if not str(request_path) or not expected:
        raise RuntimeError("Control production requires request path and exact request hash")
    request = load_control_request(request_path, expected)
    runtime_root = Path(os.environ.get("RUNNER_TEMP") or ".") / "isco-control-runtime" / str(request.get("request_id") or "request")
    execute_bundle(request, runtime_root=runtime_root)


if __name__ == "__main__":
    main()
