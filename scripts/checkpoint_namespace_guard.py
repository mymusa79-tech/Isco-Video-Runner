from __future__ import annotations

import hashlib
import json
import os

import scripts.task_level_planner_router as router


# The router checkpoint document is consumed by planning_checkpoint_state_core, whose
# authenticated persistence contract is schema version 1. Namespace evolution is a
# separate concern: changing the namespace recipe must invalidate stale responses
# without silently changing the durable document schema.
CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_NAMESPACE_SCHEMA_VERSION = 2
_MARKER = "_isco_checkpoint_namespace_guard"


def checkpoint_namespace() -> str:
    payload = {
        "schema": CHECKPOINT_NAMESPACE_SCHEMA_VERSION,
        "runner_sha": (os.environ.get("GITHUB_SHA") or "local").strip(),
        "engine_sha": (os.environ.get("ISCO_ENGINE_SHA") or "local").strip(),
        "planning_contract": "bounded-output-v2",
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def install_checkpoint_namespace_guard() -> None:
    """Prevent stale planning responses from crossing runtime/schema revisions.

    A retry of the same Runner+Engine revision may reuse the checkpoint. A code or
    Engine change gets a fresh namespace, so old valid-looking JSON cannot bypass new
    validators or semantics after a deployment. The namespace recipe version is kept
    independent from the durable checkpoint document version so both live layers share
    one explicit contract.
    """
    current_load = router._load_checkpoint
    current_save = router._save_checkpoint
    if getattr(current_load, _MARKER, False):
        return

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
