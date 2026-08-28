from __future__ import annotations

import ast
import inspect
from pathlib import Path

from scripts import planning_batch_hardening as batching
from scripts import provider_capacity_hardening as capacity
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


def _bind_contract(callable_obj, /, *args, label: str, **kwargs) -> None:
    """Validate final live wrapper-call compatibility without executing provider code."""
    try:
        signature = inspect.signature(callable_obj, follow_wrapped=False)
        signature.bind(*args, **kwargs)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"RUNTIME_CALL_CONTRACT_MISMATCH target={label} detail={exc}"
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


def repository_runtime_patch_audit() -> dict[str, object]:
    """Scan every non-test Runner script for this regression family.

    This deliberately searches the full runtime directory rather than an allowlisted
    list of Run123/124/125 files. A newly added patch file is therefore covered
    automatically and cannot evade the audit simply because nobody remembered to add it
    to another hand-maintained dependency list.
    """
    legacy: dict[str, list[str]] = {}
    patch_contracts: list[str] = []
    files = _runtime_python_files()
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found_legacy = _legacy_capacity_violations(path, tree)
        if found_legacy:
            legacy[path.name] = found_legacy
        patch_contracts.extend(_patch_assignment_violations(path, tree))

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

    return {
        "runtime_python_files_scanned": len(files),
        "legacy_capacity_violations": 0,
        "static_patch_contract_violations": 0,
    }


def certify_runtime_patch_contracts() -> dict[str, object]:
    """Certify the final composed runtime surface before production calls providers.

    Run127 proved unit tests for each patch are insufficient when multiple installers
    replace the same callable. This gate combines a full-source static audit with final
    live signature checks after the canonical installer order. It executes no provider
    request and changes no provider state.
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
    for callable_obj, args, kwargs, label in checks:
        _bind_contract(callable_obj, *args, label=label, **kwargs)

    result = {
        "status": "pass",
        "signature_checks": len(checks),
        "runtime_python_files_scanned": static["runtime_python_files_scanned"],
        "model_scoped_capacity": True,
        "legacy_capacity_authority": False,
        "static_patch_contract_violations": 0,
    }
    print(
        "Runtime patch contracts certified: "
        f"signature_checks={result['signature_checks']} "
        f"runtime_files_scanned={result['runtime_python_files_scanned']} "
        "model_scoped_capacity=true legacy_capacity_authority=false "
        "static_patch_contract_violations=0"
    )
    return result
