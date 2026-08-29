from __future__ import annotations

import hashlib
import json
from typing import Callable

import isco_video_agent.resilient_planner as staged
from scripts import task_level_planner_router as router


class PlanningCacheContractError(RuntimeError):
    """A provider/cache response is JSON but is not authoritative for its planning stage."""


def _reject(path: str, detail: str) -> None:
    raise PlanningCacheContractError(
        f"PLANNING_CACHE_CONTRACT_REJECTED schema mismatch path={path} detail={detail}"
    )


def _validate_schema(value: object, schema: dict, path: str = "$") -> None:
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(value, dict):
            _reject(path, "expected_object")
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        missing = [key for key in required if key not in value]
        if missing:
            _reject(path, "missing=" + ",".join(sorted(missing)))
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                _reject(path, "unexpected=" + ",".join(extras))
        for key, child_schema in properties.items():
            if key in value:
                _validate_schema(value[key], child_schema, f"{path}.{key}")
        return

    if expected_type == "array":
        if not isinstance(value, list):
            _reject(path, "expected_array")
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            _reject(path, f"min_items={minimum} actual={len(value)}")
        if isinstance(maximum, int) and len(value) > maximum:
            _reject(path, f"max_items={maximum} actual={len(value)}")
        child_schema = schema.get("items")
        if isinstance(child_schema, dict):
            for index, item in enumerate(value):
                _validate_schema(item, child_schema, f"{path}[{index}]")
        return

    if expected_type == "string":
        if not isinstance(value, str):
            _reject(path, "expected_string")
        return

    if expected_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _reject(path, "expected_number")
        return


def _json_array_after_marker(prompt: str, marker: str) -> list | None:
    index = prompt.find(marker)
    if index < 0:
        return None
    tail = prompt[index + len(marker):].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(tail)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, list) else None


def _explicit_id_list(prompt: str) -> list[str] | None:
    markers = (
        "using these exact ids and this exact order:",
        "using these exact ids:",
    )
    for marker in markers:
        start = 0
        while True:
            index = prompt.find(marker, start)
            if index < 0:
                break
            value = _json_array_after_marker(prompt[index:], marker)
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                return [item.strip() for item in value]
            start = index + len(marker)
    return None


def _ids_from_entry_array(prompt: str, markers: tuple[str, ...]) -> list[str] | None:
    for marker in markers:
        entries = _json_array_after_marker(prompt, marker)
        if not isinstance(entries, list):
            continue
        ids: list[str] = []
        for item in entries:
            if not isinstance(item, dict):
                return None
            section_id = str(item.get("id") or "").strip()
            if not section_id:
                return None
            ids.append(section_id)
        return ids
    return None


def _expected_ids(prompt: str, contract_name: str) -> list[str]:
    explicit = _explicit_id_list(prompt)
    if explicit:
        return explicit

    if contract_name == "full_script":
        ids = _ids_from_entry_array(
            prompt,
            (
                "Section specs (id, purpose, transition_hint) — write exactly one narration per entry, in this exact order:\n",
                "BATCH_SECTION_SPECS — write exactly one narration per entry in this exact order:\n",
                "\nSECTIONS:\n",
                "\nBATCH_SECTIONS:\n",
            ),
        )
        if ids:
            return ids
    elif contract_name == "append_only_repair":
        ids = _ids_from_entry_array(prompt, ("\nTARGET_SECTIONS:\n",))
        if ids:
            return ids

    _reject("$", f"{contract_name}_expected_ids_unresolved")
    raise AssertionError("unreachable")


def _require_unique_nonempty_ids(entries: list, *, label: str) -> list[str]:
    ids: list[str] = []
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            _reject(f"$.{label}[{index}]", "expected_object")
        section_id = str(item.get("id") or "").strip()
        if not section_id:
            _reject(f"$.{label}[{index}].id", "empty_id")
        ids.append(section_id)
    if len(ids) != len(set(ids)):
        _reject(f"$.{label}", "duplicate_ids")
    return ids


def _validate_outline_semantics(data: dict) -> None:
    briefs = data.get("section_briefs")
    if not isinstance(briefs, list):
        _reject("$.section_briefs", "expected_array")
    _require_unique_nonempty_ids(briefs, label="section_briefs")
    if any(not str(item.get("purpose") or "").strip() for item in briefs):
        _reject("$.section_briefs", "empty_purpose")

    narrative_format = str(data.get("narrative_format") or "").strip()
    allowed = getattr(staged, "_NARRATIVE_FORMATS", {})
    if narrative_format not in allowed:
        _reject("$.narrative_format", "unsupported")
    format_flags = staged.validate_narrative_format(narrative_format, n=6)
    if format_flags:
        _reject("$.narrative_format", "anti_repetition")

    opener = str(data.get("opener_variant") or "").strip()
    closer = str(data.get("closer_variant") or "").strip()
    transitions = data.get("transition_variants")
    if not opener or not closer:
        _reject("$", "empty_identity_variant")
    if not isinstance(transitions, list) or len(transitions) != 3 or any(
        not str(item).strip() for item in transitions
    ):
        _reject("$.transition_variants", "invalid_transitions")
    identity_flags = staged.validate_identity_phrases(opener, closer, n=6)
    if identity_flags:
        _reject("$", "identity_anti_repetition")
    try:
        staged.intent_from_dict(data.get("editorial_intent"))
    except Exception as exc:
        _reject("$.editorial_intent", type(exc).__name__)


def validate_response_for_cache(data: dict, prompt: str) -> str | None:
    """Return the recognized contract name only after full local authority validation.

    `None` means this is an unknown/legacy JSON task. It may execute live, but it is
    deliberately not allowed to remain in the durable planning checkpoint.
    """
    contract = router._structured_schema_for_prompt(prompt)
    if contract is None:
        return None
    contract_name, schema = contract
    _validate_schema(data, schema)

    if contract_name == "editorial_outline":
        _validate_outline_semantics(data)
    elif contract_name == "full_script":
        expected_ids = _expected_ids(prompt, contract_name)
        staged._parse_full_script_response(data, expected_ids)
    elif contract_name == "append_only_repair":
        expected_ids = _expected_ids(prompt, contract_name)
        staged._parse_append_only_response(data, expected_ids)
    elif contract_name == "section_repair":
        if not str(data.get("narration") or "").strip():
            _reject("$.narration", "empty_narration")
    else:
        _reject("$", f"unsupported_contract={contract_name}")
    return contract_name


def _effective_prompt(prompt: str) -> str:
    enriched = router._enrich_dialogue_prompt(prompt)
    return router.with_channel_persona(enriched)


def _cache_key(prompt: str, model: str) -> str:
    effective = _effective_prompt(prompt)
    return hashlib.sha256((model + "\n" + effective).encode("utf-8")).hexdigest()


def _router_cache_state(task_router: Callable, _seen: set[int] | None = None) -> tuple[dict, dict] | None:
    """Find the task_router cache even when later installers wrap json_text."""
    seen = set() if _seen is None else _seen
    identity = id(task_router)
    if identity in seen:
        return None
    seen.add(identity)

    closure = getattr(task_router, "__closure__", None)
    freevars = getattr(getattr(task_router, "__code__", None), "co_freevars", ())
    if closure and freevars and len(closure) == len(freevars):
        values = {name: cell.cell_contents for name, cell in zip(freevars, closure)}
        responses = values.get("responses")
        checkpoint = values.get("checkpoint")
        if isinstance(responses, dict) and isinstance(checkpoint, dict):
            return responses, checkpoint

        # Later planning installers may wrap the original task_router in another
        # closure. Traverse callable cells only; never inspect arbitrary data values.
        for value in values.values():
            if callable(value):
                nested = _router_cache_state(value, seen)
                if nested is not None:
                    return nested

    for attr in ("__wrapped__", "_isco_planning_cache_authority_read_original"):
        value = getattr(task_router, attr, None)
        if callable(value):
            nested = _router_cache_state(value, seen)
            if nested is not None:
                return nested
    return None


def _evict_live_entry(task_router: Callable, cache_key: str, *, reason: str) -> None:
    state = _router_cache_state(task_router)
    if state is None:
        checkpoint = router._load_checkpoint()
        responses = checkpoint.get("responses")
        if not isinstance(responses, dict):
            return
    else:
        responses, checkpoint = state
    if cache_key not in responses:
        return
    responses.pop(cache_key, None)
    router._save_checkpoint(checkpoint)
    print(f"Planning checkpoint evicted non-authoritative response: {reason}")


def install_cache_authority_pre_router() -> None:
    """Validate live provider output inside task_router before it can be cached."""
    current = router._normalize_outline
    if getattr(current, "_isco_planning_cache_authority", False):
        return

    def authoritative_normalize(data: dict, prompt: str) -> dict:
        normalized = current(data, prompt)
        validate_response_for_cache(normalized, prompt)
        return normalized

    authoritative_normalize._isco_planning_cache_authority = True
    authoritative_normalize._isco_planning_cache_authority_original = current
    router._normalize_outline = authoritative_normalize


def install_cache_authority_post_router() -> None:
    """Revalidate restored cache hits and evict anything not locally authoritative."""
    current = staged.json_text
    if getattr(current, "_isco_planning_cache_authority_read", False):
        return

    def authoritative_json_text(api_key, prompt, model="gemini-2.5-flash"):
        effective = _effective_prompt(prompt)
        key = hashlib.sha256((model + "\n" + effective).encode("utf-8")).hexdigest()
        state = _router_cache_state(current)
        cached = state[0].get(key) if state is not None else None
        if isinstance(cached, dict):
            try:
                contract_name = validate_response_for_cache(cached, effective)
            except Exception as exc:
                _evict_live_entry(current, key, reason=f"invalid_restored:{type(exc).__name__}")
            else:
                if contract_name is None:
                    _evict_live_entry(current, key, reason="unknown_restored_contract")

        result = current(api_key, prompt, model=model)
        if not isinstance(result, dict):
            _evict_live_entry(current, key, reason="non_object_result")
            raise PlanningCacheContractError("PLANNING_CACHE_CONTRACT_REJECTED schema mismatch path=$ detail=non_object_result")
        try:
            contract_name = validate_response_for_cache(result, effective)
        except Exception:
            _evict_live_entry(current, key, reason="post_read_validation_failed")
            raise
        if contract_name is None:
            _evict_live_entry(current, key, reason="unknown_live_contract")
        return result

    authoritative_json_text._isco_planning_cache_authority_read = True
    authoritative_json_text._isco_planning_cache_authority_read_original = current
    staged.json_text = authoritative_json_text


def install_planning_cache_authority() -> None:
    """Install write-side validation and read-side eviction around the live planner router."""
    install_cache_authority_pre_router()
    install_cache_authority_post_router()
