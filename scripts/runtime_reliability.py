from __future__ import annotations

import importlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

import isco_video_agent.opening_director as opening_director
import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.resilient_planner as staged
from isco_video_agent.security import safe_error
import scripts.append_retry_guard as append_guard
import scripts.task_level_planner_router as planner_router


class FailureClass(StrEnum):
    TRANSIENT_PROVIDER = "transient_provider"
    PROVIDER_OVERLOAD = "provider_overload"
    PERMANENT_CONFIG = "permanent_config"
    SCHEMA_INVALID = "schema_invalid"
    SEMANTIC_INVALID = "semantic_invalid"
    BUDGET_EXHAUSTED = "budget_exhausted"
    RESOURCE_UNAVAILABLE = "resource_unavailable"
    RUNTIME_CONTRACT = "runtime_contract"
    MEDIA_FAILURE = "media_failure"
    QUALITY_BLOCK = "quality_block"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True)
class FailurePolicy:
    failure_class: FailureClass
    retryable: bool
    owner: str


_FAILURE_POLICIES: tuple[tuple[tuple[str, ...], FailurePolicy], ...] = (
    (("429", "quota", "rate limit"), FailurePolicy(FailureClass.PROVIDER_OVERLOAD, True, "provider_router")),
    (("timeout", "timed out", "connection", "network", "http 500", "http 502", "http 503", "http 504"), FailurePolicy(FailureClass.TRANSIENT_PROVIDER, True, "provider_router")),
    (("401", "403", "unauthorized", "forbidden", "authentication", "invalid api key", "bad request", "invalid argument"), FailurePolicy(FailureClass.PERMANENT_CONFIG, False, "preflight_or_provider_router")),
    (("invalid json", "complete json object", "outline_schema_invalid", "missing id", "duplicated section id", "exact section ids"), FailurePolicy(FailureClass.SCHEMA_INVALID, True, "schema_repair")),
    (("under_section_floor", "over_section_ceiling", "over_max_append", "aggregate_overflow", "required final section band", "pre-tts duration contract failed", "under-length", "underlength"), FailurePolicy(FailureClass.SEMANTIC_INVALID, True, "bounded_output_recovery")),
    (("ai budget authorization denied", "budget exhausted", "provider_attempt_hard_cap"), FailurePolicy(FailureClass.BUDGET_EXHAUSTED, False, "budget_ledger")),
    (("no safe/relevant candidate", "fewer than three distinct", "could not assemble enough distinct", "no downloadable", "opening_sequence_unavailable"), FailurePolicy(FailureClass.RESOURCE_UNAVAILABLE, True, "visual_recovery")),
    (("runtime contract", "router is not installed", "invariant failed", "installer order"), FailurePolicy(FailureClass.RUNTIME_CONTRACT, False, "runtime_preflight")),
    (("ffmpeg", "ffprobe", "decode failed", "mux failed", "av sync", "final master qc", "audio mastering failed", "render failed"), FailurePolicy(FailureClass.MEDIA_FAILURE, False, "media_pipeline")),
    (("quality gate", "editorial room gate", "factuality", "tone/naturalness", "content-quality", "content_quality", "anti-repetition", "sensitive topic"), FailurePolicy(FailureClass.QUALITY_BLOCK, False, "quality_gate")),
)


_MANIFEST_WRAPPER_ORIGINAL_ATTRS = (
    "_isco_release_transaction_original",
    "_isco_canonical_v4_original",
)


def classify_failure(exc: BaseException) -> FailurePolicy:
    detail = f"{type(exc).__name__}: {exc}".lower()
    for markers, policy in _FAILURE_POLICIES:
        if any(marker in detail for marker in markers):
            return policy
    return FailurePolicy(FailureClass.UNEXPECTED, False, "fail_closed")


def atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def production_entrypoint_modules() -> list[object]:
    """Return every live module object for run_v3_voice, including real script mode.

    Production is launched with `python ../scripts/run_v3_voice.py`, so the executing
    file is normally `__main__`. Import-based tests use `scripts.run_v3_voice`. Python
    can keep those as two distinct module objects; patching only the package import can
    therefore produce a green test while leaving the real script entrypoint unpatched.
    """
    package_module = importlib.import_module("scripts.run_v3_voice")
    modules: list[object] = [package_module]
    main_module = sys.modules.get("__main__")
    package_file = getattr(package_module, "__file__", None)
    main_file = getattr(main_module, "__file__", None) if main_module is not None else None
    if package_file and main_file:
        try:
            same_file = Path(package_file).resolve() == Path(main_file).resolve()
        except OSError:
            same_file = False
        if same_file and main_module is not package_module:
            modules.append(main_module)
    return modules


def manifest_wrapper_chain_has_marker(value: object, marker: str) -> bool:
    """Detect one manifest guard anywhere in the known wrapper chain.

    Runtime closure is intentionally safe to call more than once. The release guard
    sits outside the canonical-bundle guard, so checking only the outer callable loses
    the inner marker and alternately stacks both wrappers on each reinstall. Follow the
    explicit original links instead; cycles or unknown wrapper shapes fail closed by
    returning False rather than looping forever.
    """
    current = value
    seen: set[int] = set()
    while callable(current) and id(current) not in seen:
        if getattr(current, marker, False):
            return True
        seen.add(id(current))
        next_value = None
        for attr in _MANIFEST_WRAPPER_ORIGINAL_ATTRS:
            candidate = getattr(current, attr, None)
            if callable(candidate) and candidate is not current:
                next_value = candidate
                break
        if next_value is None:
            return False
        current = next_value
    return False


def _artifact_presence(out_dir: Path) -> list[str]:
    known = (
        "research.json",
        "plan.json",
        "repair-dossier.json",
        "quality-precheck.json",
        "ai-budget.json",
        "visual-audit.json",
        "opening-visual-audit.json",
        "narration.wav",
        "final.mp4",
        "final-master-qc.json",
        "gold-enforce-report.json",
        "production-manifest.json",
        "delivery-manifest.json",
        "release-transaction.json",
    )
    return [name for name in known if (out_dir / name).exists()]


def _latest_output_dir() -> Path | None:
    roots = [path for path in Path("output").glob("*") if path.is_dir()]
    roots.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return roots[0] if roots else None


def write_failure_envelope(
    out_dir: Path,
    *,
    stage: str,
    exc: BaseException,
    production_id: str | None = None,
) -> Path:
    """Write stable, secret-free failure evidence without changing failure semantics."""
    policy = classify_failure(exc)
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "production_id": production_id or (os.environ.get("ISCO_PRODUCTION_ID") or None),
        "stage": stage,
        "failure_class": policy.failure_class.value,
        "retryable": policy.retryable,
        "recovery_owner": policy.owner,
        "safe_error": safe_error(exc),
        "runner_sha": (os.environ.get("GITHUB_SHA") or "").strip() or None,
        "engine_sha": (os.environ.get("ISCO_ENGINE_SHA") or "").strip() or None,
        "github_run_id": (os.environ.get("GITHUB_RUN_ID") or "").strip() or None,
        "github_run_attempt": (os.environ.get("GITHUB_RUN_ATTEMPT") or "").strip() or None,
        "artifacts_present": _artifact_presence(out_dir),
    }
    target = out_dir / "failure-envelope.json"
    atomic_write_json(target, payload)
    return target


def write_release_transaction(
    out_dir: Path,
    *,
    state: str,
    exc: BaseException | None = None,
) -> Path:
    """Journal the acceptance->delivery boundary so partial post-Gold completion is visible."""
    path = out_dir / "release-transaction.json"
    previous = _read_json_object(path)
    history = previous.get("history") if isinstance(previous.get("history"), list) else []
    event = {"state": state, "at": datetime.now(timezone.utc).isoformat()}
    if exc is not None:
        event["safe_error"] = safe_error(exc)
    history = [item for item in history[-15:] if isinstance(item, dict)] + [event]
    payload = {
        "schema_version": 1,
        "production_id": (os.environ.get("ISCO_PRODUCTION_ID") or "").strip() or None,
        "runner_sha": (os.environ.get("GITHUB_SHA") or "").strip() or None,
        "engine_sha": (os.environ.get("ISCO_ENGINE_SHA") or "").strip() or None,
        "state": state,
        "complete": state == "delivery_complete",
        "history": history,
    }
    atomic_write_json(path, payload)
    return path


def _require_marker(name: str, value: object, marker: str) -> None:
    if not getattr(value, marker, False):
        raise RuntimeError(f"Runtime contract failed: {name} missing marker {marker}")


def _assert_entrypoint_module_contract(module: object) -> None:
    name = getattr(module, "__name__", "unknown")
    manifest = getattr(module, "_write_production_manifest", None)
    _require_marker(f"{name}._write_production_manifest", manifest, "_isco_release_transaction_delivery")
    canonical = getattr(manifest, "_isco_release_transaction_original", None)
    _require_marker(f"{name}.canonical_bundle", canonical, "_isco_canonical_v4_bundle")
    _require_marker(
        f"{name}.run_gold_enforce_phase4",
        getattr(module, "run_gold_enforce_phase4", None),
        "_isco_release_transaction_gold",
    )
    _require_marker(
        f"{name}.write_planning_telemetry",
        getattr(module, "write_planning_telemetry", None),
        "_isco_reliability_telemetry_binding",
    )


def assert_runtime_contracts() -> None:
    """Fail before provider/media work if a known-critical runtime patch chain drifted."""
    _require_marker("orchestrator.build_plan", orchestrator.build_plan, "_is_resilient_router")
    _require_marker("append residual repair", append_guard._repair_all_residual_underlength, "_isco_bounded_output_recovery")
    _require_marker("append bounds validator", append_guard._validate_addition_bounds, "_isco_bounded_output_recovery_validator")
    _require_marker("full-script schema repair", staged._call_with_schema_repair, "_isco_schema_repair_policy")
    _require_marker("Gemini planning JSON adapter", planner_router.gemini_json_text, "_isco_gemini_planning_output_guard")
    _require_marker("Pexels stock search wrapper", orchestrator.pexels_search_videos, "_isco_run92_stock_pool_guard")
    _require_marker("Pixabay stock search wrapper", orchestrator.pixabay_provider.search_videos, "_isco_run92_stock_pool_guard")
    _require_marker("opening selector wrapper", opening_director.select_opening_sequence, "_isco_run92_adaptive_opening_guard")
    _require_marker("section selector stable-intent wrapper", orchestrator.select_section_sequence, "_isco_run92_stable_visual_intent")
    _require_marker("single selector stable-intent wrapper", orchestrator.select_with_recovery, "_isco_run92_stable_visual_intent")
    for module in production_entrypoint_modules():
        _assert_entrypoint_module_contract(module)


def install_core_reliability_guard() -> None:
    current = orchestrator.produce
    if getattr(current, "_isco_core_reliability_guard", False):
        return

    def guarded_produce(*args, **kwargs):
        try:
            assert_runtime_contracts()
            return current(*args, **kwargs)
        except Exception as exc:
            out_dir = _latest_output_dir()
            if out_dir is not None:
                try:
                    write_failure_envelope(out_dir, stage="core_production", exc=exc)
                except Exception as diagnostic_exc:
                    print(
                        "Failure envelope write skipped "
                        f"({type(diagnostic_exc).__name__}); preserving original failure"
                    )
            raise

    guarded_produce._isco_core_reliability_guard = True
    guarded_produce._isco_core_reliability_original = current
    orchestrator.produce = guarded_produce


def install_release_transaction_guard() -> None:
    """Journal Gold acceptance and unified delivery on every live entrypoint module."""
    for production in production_entrypoint_modules():
        current_gold = getattr(production, "run_gold_enforce_phase4")
        if not getattr(current_gold, "_isco_release_transaction_gold", False):
            def make_gold_wrapper(current):
                def guarded_gold(*args, **kwargs):
                    out_dir = Path(kwargs.get("output_dir") or args[0])
                    write_release_transaction(out_dir, state="gold_started")
                    try:
                        result = current(*args, **kwargs)
                    except Exception as exc:
                        write_release_transaction(out_dir, state="gold_failed", exc=exc)
                        try:
                            write_failure_envelope(out_dir, stage="gold_enforcement", exc=exc)
                        except Exception:
                            pass
                        raise
                    write_release_transaction(out_dir, state="gold_accepted")
                    return result
                return guarded_gold

            wrapped_gold = make_gold_wrapper(current_gold)
            wrapped_gold._isco_release_transaction_gold = True
            wrapped_gold._isco_release_transaction_original = current_gold
            setattr(production, "run_gold_enforce_phase4", wrapped_gold)

        current_manifest = getattr(production, "_write_production_manifest")
        if not manifest_wrapper_chain_has_marker(current_manifest, "_isco_release_transaction_delivery"):
            def make_manifest_wrapper(current):
                def guarded_manifest(out: Path, *, production_id: str, fmt: str):
                    out = Path(out)
                    write_release_transaction(out, state="delivery_started")
                    try:
                        result = current(out, production_id=production_id, fmt=fmt)
                    except Exception as exc:
                        write_release_transaction(out, state="post_acceptance_incomplete_delivery", exc=exc)
                        try:
                            write_failure_envelope(out, stage="post_gold_delivery", exc=exc)
                        except Exception:
                            pass
                        raise
                    write_release_transaction(out, state="delivery_complete")
                    return result
                return guarded_manifest

            wrapped_manifest = make_manifest_wrapper(current_manifest)
            wrapped_manifest._isco_release_transaction_delivery = True
            wrapped_manifest._isco_release_transaction_original = current_manifest
            setattr(production, "_write_production_manifest", wrapped_manifest)


def install_telemetry_reliability_binding() -> None:
    """Embed reliability evidence in telemetry for every live entrypoint module."""
    for production in production_entrypoint_modules():
        current = getattr(production, "write_planning_telemetry")
        if getattr(current, "_isco_reliability_telemetry_binding", False):
            continue

        def make_telemetry_wrapper(original):
            def guarded_write(out_dir: Path) -> Path:
                path = original(out_dir)
                data = _read_json_object(path)
                failure = _read_json_object(Path(out_dir) / "failure-envelope.json")
                transaction = _read_json_object(Path(out_dir) / "release-transaction.json")
                if failure:
                    data["failure_envelope"] = failure
                if transaction:
                    data["release_transaction"] = transaction
                atomic_write_json(path, data)
                return path
            return guarded_write

        wrapped = make_telemetry_wrapper(current)
        wrapped._isco_reliability_telemetry_binding = True
        wrapped._isco_reliability_telemetry_original = current
        setattr(production, "write_planning_telemetry", wrapped)
