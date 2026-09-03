from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from scripts import planning_checkpoint_state as checkpoint
from scripts.persistent_memory_core import _atomic_json_write, read_restore_identity
from scripts.runtime_phase import (
    activate_canonical_runtime,
    canonical_runtime_enabled,
    canonical_workflow_identity,
)


CONTRACT_ID = "p0.runtime-environment-state.v2"
EVIDENCE_FILENAME = "p0-runtime-master-contract.json"
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: Path, *, label: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"P0 master missing/invalid {label} evidence") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"P0 master {label} evidence must be a JSON object")
    return value


def _require_secret_file(path: Path, *, label: str) -> None:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"P0 master durable {label} is missing or unsafe")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"P0 master durable {label} is unreadable") from exc
    if not value:
        raise RuntimeError(f"P0 master durable {label} is empty")
    if path.stat().st_mode & 0o077:
        raise RuntimeError(f"P0 master durable {label} permissions are not private")


def _require_exact_runtime_identity() -> tuple[str, str, str, str]:
    runner_sha = str(os.environ.get("GITHUB_SHA") or "").strip().lower()
    engine_sha = str(
        os.environ.get("ISCO_ENGINE_SHA")
        or os.environ.get("EXPECTED_ENGINE_SHA")
        or ""
    ).strip().lower()
    run_id = str(os.environ.get("GITHUB_RUN_ID") or "").strip()
    run_attempt = str(os.environ.get("GITHUB_RUN_ATTEMPT") or "").strip()
    if not _SHA1_RE.fullmatch(runner_sha):
        raise RuntimeError("P0 master requires exact Runner SHA")
    if not _SHA1_RE.fullmatch(engine_sha):
        raise RuntimeError("P0 master requires exact Engine SHA")
    if not run_id.isdigit() or int(run_id) <= 0:
        raise RuntimeError("P0 master requires exact GitHub run id")
    if not run_attempt.isdigit() or int(run_attempt) <= 0:
        raise RuntimeError("P0 master requires exact GitHub run attempt")
    return runner_sha, engine_sha, run_id, run_attempt


def _certify_preproduction_evidence(temp: Path) -> tuple[dict, dict, dict, dict[str, str]]:
    environment_path = temp / "preproduction-environment.json"
    provider_path = temp / "provider-preflight.json"
    planning_path = temp / "planning-envelope-preflight.json"

    environment = _load_object(environment_path, label="environment")
    if environment.get("schema_version") != 2:
        raise RuntimeError("P0 master environment evidence schema mismatch")
    if environment.get("ffmpeg_libx264") is not True or environment.get("tesseract_arabic") is not True:
        raise RuntimeError("P0 master environment capability evidence is incomplete")
    filters = environment.get("ffmpeg_filters")
    required_filters = {"blackdetect", "silencedetect", "freezedetect", "loudnorm", "subtitles"}
    if not isinstance(filters, list) or not required_filters.issubset({str(item) for item in filters}):
        raise RuntimeError("P0 master environment FFmpeg evidence is incomplete")

    providers = _load_object(provider_path, label="provider readiness")
    if providers.get("schema_version") != 4 or providers.get("overall_status") != "pass":
        raise RuntimeError("P0 master provider readiness did not pass")
    if providers.get("hard_failures") not in ([], None):
        raise RuntimeError("P0 master provider readiness contains hard failures")
    checks = providers.get("checks")
    if not isinstance(checks, list):
        raise RuntimeError("P0 master provider readiness checks are missing")
    by_provider = {
        str(item.get("provider") or ""): item
        for item in checks
        if isinstance(item, dict)
    }
    for provider in ("gemini", "pexels"):
        if not isinstance(by_provider.get(provider), dict) or by_provider[provider].get("status") != "pass":
            raise RuntimeError(f"P0 master hard provider is not ready: {provider}")

    planning = _load_object(planning_path, label="planning envelope")
    if planning.get("status") != "pass":
        raise RuntimeError("P0 master planning envelope did not pass")
    required = planning.get("required_provider_families")
    families = planning.get("viable_provider_families")
    if not isinstance(required, int) or required < 2:
        raise RuntimeError("P0 master planning redundancy contract is missing")
    if not isinstance(families, list) or len({str(item) for item in families}) < required:
        raise RuntimeError("P0 master planning provider-family redundancy is not satisfied")

    hashes = {
        "environment_sha256": _sha256_file(environment_path),
        "provider_preflight_sha256": _sha256_file(provider_path),
        "planning_envelope_sha256": _sha256_file(planning_path),
    }
    return environment, providers, planning, hashes


def _certify_durable_planning_state(
    *,
    repo_root: Path,
    engine_root: Path,
    snapshot_path: Path,
    snapshot_sha: str,
    engine_sha: str,
) -> dict:
    binding = checkpoint.build_binding(
        brief_path=snapshot_path,
        expected_brief_sha256=snapshot_sha,
        engine_repo=engine_root,
        expected_engine_sha=engine_sha,
        contract_root=repo_root,
    )
    identity_path = checkpoint.runtime_identity_path()
    identity = checkpoint._read_identity(identity_path, binding)
    state_dir = identity_path.parent
    _require_secret_file(state_dir / checkpoint._RUNTIME_KEY_FILENAME, label="checkpoint key")
    _require_secret_file(state_dir / checkpoint._RUNTIME_TOKEN_FILENAME, label="checkpoint GitHub credential")
    return {
        "source": identity.get("source"),
        "state_commit": identity.get("state_commit"),
        "state_sequence": identity.get("state_sequence"),
        "binding_digest": identity.get("binding_digest"),
    }


def activate_p0_runtime_master() -> dict:
    """Perform the one live P0 phase transition after all pre-production gates pass.

    Pre-production may freeze immutable inputs and restore authenticated state in earlier
    workflow processes, but those helpers never export live-runtime authority. The final
    P0 preflight process calls this only after environment, provider-readiness, exact
    planning-envelope and memory checks have completed. Its explicit phase identity is
    then exported to subsequent workflow steps; ambient GitHub context alone is never
    sufficient.
    """
    if not canonical_workflow_identity():
        return {
            "contract_id": CONTRACT_ID,
            "decision": "not_applicable",
            "reason": "non-canonical workflow",
        }

    # Synthetic unit tests intentionally model workflow identity without a real Actions
    # run id. They are not allowed to manufacture live-runtime evidence. A genuine
    # Production V4 process always has GITHUB_RUN_ID and is fail-closed below.
    if not str(os.environ.get("GITHUB_RUN_ID") or "").strip():
        return {
            "contract_id": CONTRACT_ID,
            "decision": "not_applicable",
            "reason": "synthetic canonical context without GitHub run identity",
        }

    if canonical_runtime_enabled():
        return {
            "contract_id": CONTRACT_ID,
            "decision": "pass",
            "phase": "already_active_inherited",
        }

    temp_value = str(os.environ.get("RUNNER_TEMP") or "").strip()
    history_value = str(os.environ.get("ISCO_HISTORY_PATH") or "").strip()
    if not temp_value or not history_value:
        raise RuntimeError("P0 master requires prepared runtime temp and persistent-memory paths")
    temp = Path(temp_value).resolve()
    history = Path(history_value).resolve()
    if not history.is_file():
        raise RuntimeError("P0 master persistent memory payload is missing")

    memory_identity = read_restore_identity(history)
    environment, providers, planning, hashes = _certify_preproduction_evidence(temp)
    runner_sha, engine_sha, run_id, run_attempt = _require_exact_runtime_identity()

    snapshot_path_value = str(os.environ.get("ISCO_APPROVED_BRIEF_SNAPSHOT_PATH") or "").strip()
    snapshot_sha = str(os.environ.get("ISCO_APPROVED_BRIEF_SNAPSHOT_SHA256") or "").strip().lower()
    if not snapshot_path_value or not _SHA256_RE.fullmatch(snapshot_sha):
        raise RuntimeError("P0 master immutable approved-brief snapshot identity is missing")
    snapshot_path = Path(snapshot_path_value).resolve()
    if not snapshot_path.is_file() or snapshot_path.stat().st_mode & 0o222:
        raise RuntimeError("P0 master immutable approved-brief snapshot is missing or writable")
    if _sha256_file(snapshot_path) != snapshot_sha:
        raise RuntimeError("P0 master immutable approved-brief snapshot hash mismatch")

    repo_root = Path(__file__).resolve().parents[1]
    engine_root = repo_root / "engine"
    durable = _certify_durable_planning_state(
        repo_root=repo_root,
        engine_root=engine_root,
        snapshot_path=snapshot_path,
        snapshot_sha=snapshot_sha,
        engine_sha=engine_sha,
    )

    # Sole cross-step live-runtime promotion. GITHUB_ENV applies only to subsequent
    # steps, so this final P0 gate creates the real boundary immediately before produce.
    activate_canonical_runtime(persist_workflow_env=True)
    if not canonical_runtime_enabled():
        raise RuntimeError("P0 master failed to enter explicit live runtime")

    evidence = {
        "schema_version": 1,
        "contract_id": CONTRACT_ID,
        "decision": "pass",
        "runtime_phase": "canonical_live",
        "runner_sha": runner_sha,
        "engine_sha": engine_sha,
        "github_run_id": run_id,
        "github_run_attempt": run_attempt,
        "persistent_memory_source": memory_identity.get("source"),
        "persistent_memory_state_commit": memory_identity.get("state_commit"),
        "durable_planning_source": durable.get("source"),
        "durable_planning_state_commit": durable.get("state_commit"),
        "durable_planning_state_sequence": durable.get("state_sequence"),
        "durable_planning_binding_digest": durable.get("binding_digest"),
        "approved_brief_snapshot_sha256": snapshot_sha,
        **hashes,
        "environment_release_namespace": environment.get("release_namespace"),
        "provider_fallback_degraded": providers.get("fallback_degraded", []),
        "planning_format": planning.get("format"),
        "planning_provider_families": planning.get("viable_provider_families", []),
    }
    _atomic_json_write(temp / EVIDENCE_FILENAME, evidence)
    print(
        "P0 Runtime Environment + State Master PASS: "
        f"runner={runner_sha[:12]} engine={engine_sha[:12]} run={run_id}/{run_attempt}"
    )
    return evidence