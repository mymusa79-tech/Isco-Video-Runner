from __future__ import annotations

import ast
import inspect
from pathlib import Path

from scripts import planning_batch_hardening as batching
from scripts import provider_capacity_hardening as capacity
from scripts import research_provider_reliability as research_reliability
from scripts import run125_capacity_routing_closure as run125
from scripts import task_level_planner_router as router


SCRIPTS_DIR = Path(__file__).resolve().parent

# provider_capacity_hardening.py owns these compatibility symbols and mirrors current
# evidence there for old diagnostics/tests. Every other non-test runtime script must use
# the model-scoped API instead of reading them as authority.
_FORBIDDEN_RUNTIME_CAPACITY_SYMBOLS = frozenset(
    {
        "_GROQ_RATE_STATE",
        "GROQ_FREE_TPM_LIMIT",
    }
)
_LEGACY_CAPACITY_OWNER = "provider_capacity_hardening.py"

# Canonical production keeps historical helper functions import-compatible, but only
# these explicit owners may assign the authority-bearing router seams.  A new runtime
# patch cannot regain schema/checkpoint ownership without failing the repository-wide
# audit before any provider call.
_PLANNING_AUTHORITY_ASSIGNMENT_OWNERS = {
    "_structured_schema_for_prompt": frozenset({"planning_stage_contract.py"}),
    "_load_checkpoint": frozenset(
        {"checkpoint_namespace_guard.py", "planning_legacy_authority_guard.py"}
    ),
    "_save_checkpoint": frozenset(
        {"checkpoint_namespace_guard.py", "planning_legacy_authority_guard.py"}
    ),
}
_QUARANTINED_PROMPT_SCHEMA_DEFINITION_OWNER = "task_level_planner_router.py"

# These three files contain historical `min(...RETRY_AFTER...)` source expressions.
# They are not allowed to multiply: final production composition makes the value a
# WAIT BUDGET (fail over when provider evidence exceeds it), and the live contract below
# proves no partial same-provider retry survives. A new source site anywhere else fails
# the repository-wide audit before provider work.
_KNOWN_NEUTRALIZED_RETRY_AFTER_MIN_FILES = frozenset(
    {
        "provider_capacity_hardening.py",
        "run123_planning_latency_hardening.py",
        "task_level_planner_router.py",
    }
)

# High-risk runtime extension points that are replaced by multiple installers. The
# value describes how the production caller invokes the final callable: positional
# argument count plus required keyword names. Static validation is intentionally about
# call compatibility, not identical parameter spelling beyond required keywords.
_PATCH_CALL_SHAPES: dict[str, tuple[int, frozenset[str]]] = {
    "_proactive_groq_pacing": (1, frozenset({"model_name"})),
    "_call_capacity_aware_shard": (3, frozenset({"prompt_builder", "label"})),
    "_groq_model_call": (2, frozenset()),
    "_groq_call": (1, frozenset()),
    "_openrouter_structured_request": (2, frozenset()),
    "_record_attempt": (2, frozenset()),
    "_extract_response_meta": (2, frozenset()),
}


def _bind_contract(callable_obj, /, *args, contract_label: str, **kwargs) -> None:
    """Validate final live wrapper-call compatibility without executing provider code."""
    try:
        signature = inspect.signature(callable_obj, follow_wrapped=False)
        signature.bind(*args, **kwargs)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"RUNTIME_CALL_CONTRACT_MISMATCH target={contract_label} detail={exc}"
        ) from exc


def _runtime_python_files() -> list[Path]:
    return sorted(
        path
        for path in SCRIPTS_DIR.glob("*.py")
        if not path.name.startswith("test_") and path.name != "__init__.py"
    )


def _legacy_capacity_violations(path: Path, tree: ast.AST) -> list[str]:
    if path.name == _LEGACY_CAPACITY_OWNER:
        return []
    violations: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_RUNTIME_CAPACITY_SYMBOLS:
            violations.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_RUNTIME_CAPACITY_SYMBOLS:
            violations.add(node.attr)
    return sorted(violations)


def _function_defs(tree: ast.AST) -> dict[str, ast.arguments]:
    result: dict[str, ast.arguments] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result[node.name] = node.args
    return result


def _accepts_call_shape(
    args: ast.arguments,
    *,
    positional_count: int,
    keyword_names: frozenset[str],
) -> bool:
    positional = [*args.posonlyargs, *args.args]
    if positional_count > len(positional) and args.vararg is None:
        return False

    # Positional defaults align to the final N positional parameters.
    required_positional_count = len(positional) - len(args.defaults)
    consumed_required = min(positional_count, required_positional_count)
    if consumed_required < required_positional_count:
        missing_required = {
            param.arg
            for param in positional[positional_count:required_positional_count]
            if param.arg not in keyword_names
        }
        if missing_required:
            return False

    posonly_names = {param.arg for param in args.posonlyargs}
    keyword_capable = {param.arg for param in args.args}
    keyword_capable.update(param.arg for param in args.kwonlyargs)
    for name in keyword_names:
        if name in posonly_names:
            return False
        if name not in keyword_capable and args.kwarg is None:
            return False

    required_kwonly = {
        param.arg
        for param, default in zip(args.kwonlyargs, args.kw_defaults)
        if default is None
    }
    if not required_kwonly.issubset(keyword_names):
        return False
    return True


def _patch_assignment_violations(path: Path, tree: ast.AST) -> list[str]:
    defs = _function_defs(tree)
    violations: list[str] = []

    assignments: list[tuple[ast.Attribute, ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute):
                    assignments.append((target, node.value))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Attribute):
            assignments.append((node.target, node.value))

    for target, value in assignments:
        shape = _PATCH_CALL_SHAPES.get(target.attr)
        if shape is None or not isinstance(value, ast.Name):
            continue
        function_args = defs.get(value.id)
        if function_args is None:
            # Imported/external callables are not statically guessed here. The final
            # composed runtime surface is separately checked with inspect.signature.
            continue
        positional_count, keyword_names = shape
        if not _accepts_call_shape(
            function_args,
            positional_count=positional_count,
            keyword_names=keyword_names,
        ):
            violations.append(
                f"{path.name}:{target.attr}<-{value.id}"
            )
    return violations


def _planning_authority_assignment_violations(path: Path, tree: ast.AST) -> list[str]:
    violations: list[str] = []
    sites: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets.extend(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets.append(node.target)
        for target in targets:
            if isinstance(target, ast.Attribute):
                sites.append((target.attr, getattr(target, "lineno", 0)))

        # Catch dynamic setattr-based replacement as well as ordinary assignments.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "setattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            sites.append((node.args[1].value, getattr(node, "lineno", 0)))

    for attribute, line in sites:
        owners = _PLANNING_AUTHORITY_ASSIGNMENT_OWNERS.get(attribute)
        if owners is not None and path.name not in owners:
            violations.append(f"{path.name}:{line}:{attribute}")
    return violations


def _legacy_prompt_contract_inference_violations(path: Path, tree: ast.AST) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name == "_contract_name_for_prompt":
            violations.append(f"{path.name}:{node.lineno}:{node.name}")
        if (
            node.name == "_structured_schema_for_prompt"
            and path.name != _QUARANTINED_PROMPT_SCHEMA_DEFINITION_OWNER
        ):
            violations.append(f"{path.name}:{node.lineno}:{node.name}")
    return violations


def _retry_after_min_sites(path: Path, tree: ast.AST) -> list[str]:
    """Find source expressions that can turn a provider minimum delay into a cap."""
    sites: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "min":
            continue
        try:
            text = ast.unparse(node).casefold()
        except Exception:
            text = ""
        if "retry_after" in text:
            sites.append(f"{path.name}:{getattr(node, 'lineno', 0)}")
    return sites


def repository_runtime_patch_audit() -> dict[str, object]:
    """Scan every non-test Runner script for capacity, patch and retry-policy drift.

    This deliberately searches the full runtime directory rather than an allowlisted
    list of Run123/124/125 files. A newly added patch/retry file is therefore covered
    automatically and cannot evade the audit simply because nobody remembered to add it
    to another hand-maintained dependency list.
    """
    legacy: dict[str, list[str]] = {}
    patch_contracts: list[str] = []
    planning_authority_assignments: list[str] = []
    legacy_prompt_contract_inference: list[str] = []
    retry_after_min_sites: list[str] = []
    files = _runtime_python_files()
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found_legacy = _legacy_capacity_violations(path, tree)
        if found_legacy:
            legacy[path.name] = found_legacy
        patch_contracts.extend(_patch_assignment_violations(path, tree))
        planning_authority_assignments.extend(
            _planning_authority_assignment_violations(path, tree)
        )
        legacy_prompt_contract_inference.extend(
            _legacy_prompt_contract_inference_violations(path, tree)
        )
        retry_after_min_sites.extend(_retry_after_min_sites(path, tree))

    if legacy:
        detail = "; ".join(
            f"{name}:{','.join(symbols)}" for name, symbols in sorted(legacy.items())
        )
        raise RuntimeError(
            "RUNTIME_CAPACITY_AUTHORITY_DRIFT "
            f"legacy_symbols_in_runtime={detail}"
        )
    if patch_contracts:
        raise RuntimeError(
            "RUNTIME_STATIC_PATCH_CONTRACT_MISMATCH "
            + " | ".join(sorted(patch_contracts))
        )
    if planning_authority_assignments:
        raise RuntimeError(
            "RUNTIME_PLANNING_AUTHORITY_ASSIGNMENT_DRIFT "
            + " | ".join(sorted(planning_authority_assignments))
        )
    if legacy_prompt_contract_inference:
        raise RuntimeError(
            "RUNTIME_PROMPT_CONTRACT_INFERENCE_DRIFT "
            + " | ".join(sorted(legacy_prompt_contract_inference))
        )

    unexpected_retry_sites = sorted(
        site
        for site in retry_after_min_sites
        if site.split(":", 1)[0] not in _KNOWN_NEUTRALIZED_RETRY_AFTER_MIN_FILES
    )
    if unexpected_retry_sites:
        raise RuntimeError(
            "RUNTIME_PARTIAL_RETRY_AFTER_SOURCE_DRIFT "
            + " | ".join(unexpected_retry_sites)
        )

    return {
        "runtime_python_files_scanned": len(files),
        "legacy_capacity_violations": 0,
        "static_patch_contract_violations": 0,
        "planning_authority_assignment_violations": 0,
        "legacy_prompt_contract_inference_violations": 0,
        "retry_after_min_sites": len(retry_after_min_sites),
        "unexpected_retry_after_min_sites": 0,
    }


def _certify_retry_after_contracts() -> int:
    """Prove final Planning + Research composition cannot partially honor Retry-After.

    Run128 exposed a cross-generation policy conflict: an old local latency cap shortened
    a real provider Retry-After and re-issued the same request inside the still-busy TPM
    window. This check runs after all planning installers, makes no network call, and
    verifies both live retry owners against the exact dangerous shape before production
    can spend provider time or quota.
    """
    saved_headers = dict(router._last_call_rate_limit_headers)
    try:
        router._last_call_rate_limit_headers.clear()
        router._last_call_rate_limit_headers["retry_after"] = "38"
        planning_failure = router.classify_provider_failure(
            "groq",
            RuntimeError(
                "GROQ_HTTP_429 status=429 code=rate_limit_exceeded "
                "message=Rate limit reached on tokens per minute (TPM): Limit 8000"
            ),
        )
        if planning_failure.telemetry_result == "429" or planning_failure.open_circuit:
            raise RuntimeError(
                "RUNTIME_RETRY_AFTER_CONTRACT_MISMATCH "
                "target=planning expected=failover_without_partial_retry"
            )

        research_delay = research_reliability._backoff_seconds(
            RuntimeError("HTTP 429 rate_limit_exceeded. Please retry in 38s."),
            attempt=1,
        )
        if research_delay != 38.0:
            raise RuntimeError(
                "RUNTIME_RETRY_AFTER_CONTRACT_MISMATCH "
                "target=research expected=full_provider_retry_after_within_budget"
            )
    finally:
        router._last_call_rate_limit_headers.clear()
        router._last_call_rate_limit_headers.update(saved_headers)
    return 2


def certify_runtime_patch_contracts() -> dict[str, object]:
    """Certify final composed runtime behavior before production calls providers.

    Run127 proved unit tests for each patch are insufficient when multiple installers
    replace the same callable. Run128 then proved that semantic policy composition must
    also be certified: a callable can have a valid signature yet still combine an old
    retry cap with newer provider evidence incorrectly. This gate therefore combines a
    full-source static audit, final live signature checks, and provider-free Retry-After
    policy probes after the canonical installer order.
    """
    static = repository_runtime_patch_audit()
    model_20b = "openai/gpt-oss-20b"
    model_120b = "openai/gpt-oss-120b"
    request_capacity = {
        "estimated_request_tokens": 1,
        "contract": "json_object",
    }

    checks = (
        (
            capacity._proactive_groq_pacing,
            (request_capacity,),
            {"model_name": model_20b},
            "capacity._proactive_groq_pacing",
        ),
        (
            run125._groq_model_call,
            ("contract-probe", model_120b),
            {},
            "run125._groq_model_call",
        ),
        (
            batching._call_capacity_aware_shard,
            ("key", "model", ["S1"]),
            {"prompt_builder": lambda _ids: "probe", "label": "writer"},
            "batching._call_capacity_aware_shard",
        ),
        (
            router._openrouter_structured_request,
            ("probe", ("json_object", {})),
            {},
            "router._openrouter_structured_request",
        ),
        (
            router._record_attempt,
            ("groq", "success"),
            {"duration_seconds": 0.0, "provider_attempt": 1},
            "router._record_attempt",
        ),
        (
            router._extract_response_meta,
            ({}, {}),
            {},
            "router._extract_response_meta",
        ),
    )
    for callable_obj, args, kwargs, contract_label in checks:
        _bind_contract(callable_obj, *args, contract_label=contract_label, **kwargs)

    retry_after_checks = _certify_retry_after_contracts()
    result = {
        "status": "pass",
        "signature_checks": len(checks),
        "retry_after_checks": retry_after_checks,
        "runtime_python_files_scanned": static["runtime_python_files_scanned"],
        "known_neutralized_retry_after_min_sites": static["retry_after_min_sites"],
        "model_scoped_capacity": True,
        "legacy_capacity_authority": False,
        "partial_retry_after": False,
        "static_patch_contract_violations": 0,
        "planning_authority_assignment_violations": 0,
        "legacy_prompt_contract_inference_violations": 0,
    }
    print(
        "Runtime patch contracts certified: "
        f"signature_checks={result['signature_checks']} "
        f"retry_after_checks={result['retry_after_checks']} "
        f"runtime_files_scanned={result['runtime_python_files_scanned']} "
        f"known_retry_after_min_sites={result['known_neutralized_retry_after_min_sites']} "
        "model_scoped_capacity=true legacy_capacity_authority=false "
        "partial_retry_after=false static_patch_contract_violations=0"
        " planning_authority_assignment_violations=0"
        " legacy_prompt_contract_inference_violations=0"
    )
    return result
