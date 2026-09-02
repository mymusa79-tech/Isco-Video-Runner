from __future__ import annotations

import hashlib
import json
import os

from scripts import planning_checkpoint_state as durable_state
from scripts import planning_stage_contract as stage_contract
import scripts.task_level_planner_router as router


# The router checkpoint document is consumed by the authenticated durable-persistence
# layer, whose document contract is schema version 1. Namespace/cache evolution is a
# separate concern: changing logical authority must invalidate stale responses without
# silently changing the durable document schema.
CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_NAMESPACE_SCHEMA_VERSION = 2
STAGE_CACHE_LOGICAL_VERSION = 2
STAGE_CACHE_VERSION_FIELD = "stage_contract_cache_version"
_MARKER = "_isco_checkpoint_namespace_guard"
_STAGE_MARKER = "_isco_stage_checkpoint_namespace_guard"


def checkpoint_namespace() -> str:
    payload = {
        "schema": CHECKPOINT_NAMESPACE_SCHEMA_VERSION,
        "runner_sha": (os.environ.get("GITHUB_SHA") or "local").strip(),
        "engine_sha": (os.environ.get("ISCO_ENGINE_SHA") or "local").strip(),
        "planning_contract": "bounded-output-v2",
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _fresh_stage_cache() -> dict:
    return {"version": STAGE_CACHE_LOGICAL_VERSION, "responses": {}}


def _stage_checkpoint_load() -> dict:
    """Expose logical Stage-v2 authority while retaining durable document v1 bytes."""
    path = router.CACHE_PATH
    if not path.exists():
        return _fresh_stage_cache()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.CHECKPOINT_INVALID,
            f"checkpoint_json_unreadable:{type(exc).__name__}",
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("responses", {}), dict):
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.CHECKPOINT_INVALID,
            "checkpoint_root_shape_invalid",
        )

    document_version = data.get("version")
    logical_version = data.get(STAGE_CACHE_VERSION_FIELD)

    # Run170 pre-fix could create an ephemeral v2 document before authenticated
    # failure-save rejected it. It can never arrive from a successful durable restore,
    # so accept it only as Stage-v2 authority and migrate it on the next Stage write.
    if document_version == STAGE_CACHE_LOGICAL_VERSION:
        return {
            "version": STAGE_CACHE_LOGICAL_VERSION,
            "responses": dict(data.get("responses", {})),
        }

    # A historical durable-v1 document without the Stage-v2 marker belongs to older
    # prompt-hash authority. Never silently promote those responses into Stage Contract.
    if (
        document_version != CHECKPOINT_SCHEMA_VERSION
        or logical_version != STAGE_CACHE_LOGICAL_VERSION
    ):
        return _fresh_stage_cache()

    return {
        "version": STAGE_CACHE_LOGICAL_VERSION,
        "responses": dict(data.get("responses", {})),
    }


def _stage_checkpoint_save(checkpoint: dict) -> None:
    """Write logical Stage-v2 cache inside the authenticated durable-v1 document."""
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("version") != STAGE_CACHE_LOGICAL_VERSION
        or not isinstance(checkpoint.get("responses"), dict)
    ):
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.CHECKPOINT_INVALID,
            "stage_checkpoint_shape_invalid",
        )

    payload = {
        "version": CHECKPOINT_SCHEMA_VERSION,
        STAGE_CACHE_VERSION_FIELD: STAGE_CACHE_LOGICAL_VERSION,
        "responses": dict(checkpoint["responses"]),
    }
    try:
        # Run130 authority rule: validate through the public checkpoint wrapper, never
        # by importing the compatibility core directly. This catches future durable
        # schema drift at the Stage writer instead of waiting until run-end persistence.
        durable_state._normalize_checkpoint(payload)
    except (OSError, ValueError) as exc:
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.CHECKPOINT_INVALID,
            f"durable_checkpoint_contract_rejected:{type(exc).__name__}:{str(exc)[:240]}",
        ) from exc

    path = router.CACHE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


setattr(_stage_checkpoint_load, _STAGE_MARKER, True)
setattr(_stage_checkpoint_save, _STAGE_MARKER, True)


def install_checkpoint_namespace_guard() -> None:
    """Compose legacy namespace isolation and explicit Stage durable cache authority.

    Run132 owns the checkpoint document/logical namespace boundary. Run170 extends that
    same owner to the newer Explicit Stage Contract cache: Stage remains logical v2,
    authenticated durable persistence remains document v1, and the two contracts share
    the same bytes without either layer pretending to own the other's schema version.
    """
    current_load = router._load_checkpoint
    current_save = router._save_checkpoint
    if not getattr(current_load, _MARKER, False):
        def guarded_load() -> dict:
            expected = checkpoint_namespace()
            data = current_load()
            if not isinstance(data, dict) or data.get("namespace") != expected:
                return {
                    "version": CHECKPOINT_SCHEMA_VERSION,
                    "namespace": expected,
                    "responses": {},
                }
            fixed = dict(data)
            fixed["version"] = CHECKPOINT_SCHEMA_VERSION
            fixed["namespace"] = expected
            responses = fixed.get("responses")
            if not isinstance(responses, dict):
                fixed["responses"] = {}
            return fixed

        def guarded_save(data: dict) -> None:
            fixed = dict(data)
            fixed["version"] = CHECKPOINT_SCHEMA_VERSION
            fixed["namespace"] = checkpoint_namespace()
            if not isinstance(fixed.get("responses"), dict):
                fixed["responses"] = {}
            current_save(fixed)

        setattr(guarded_load, _MARKER, True)
        setattr(guarded_save, _MARKER, True)
        router._load_checkpoint = guarded_load
        router._save_checkpoint = guarded_save

    # Run127 explicitly reserves checkpoint-authority reassignment to this established
    # owner. Do not move these assignments into a new runtime patch module.
    if not getattr(stage_contract._load_checkpoint_strict, _STAGE_MARKER, False):
        stage_contract._load_checkpoint_strict = _stage_checkpoint_load
        stage_contract._save_checkpoint = _stage_checkpoint_save


def assert_stage_checkpoint_namespace_guarded() -> None:
    if not getattr(stage_contract._load_checkpoint_strict, _STAGE_MARKER, False):
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            "Stage checkpoint load authority is not composed with durable v1",
        )
    if not getattr(stage_contract._save_checkpoint, _STAGE_MARKER, False):
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            "Stage checkpoint save authority is not composed with durable v1",
        )
