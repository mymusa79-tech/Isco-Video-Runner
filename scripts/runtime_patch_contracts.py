from __future__ import annotations

import ast
import inspect
from pathlib import Path

from scripts import planning_batch_hardening as batching
from scripts import provider_capacity_hardening as capacity
from scripts import run123_planning_latency_hardening as run123
from scripts import run124_terminal_provider_recovery as run124
from scripts import run125_capacity_routing_closure as run125
from scripts import dynamic_planning_capacity as dynamic
from scripts import task_level_planner_router as router


# These modules are allowed to consume the canonical model-scoped capacity API but must
# never directly read the compatibility mirror or the historical fixed-limit alias.
# provider_capacity_hardening.py itself is excluded because it owns both compatibility
# symbols and mirrors provider evidence there for old diagnostics/tests only.
_CAPACITY_CONSUMER_MODULES = (
    batching,
    run123,
    run124,
    run125,
    dynamic,
)
_FORBIDDEN_RUNTIME_CAPACITY_SYMBOLS = frozenset(
    {
        "_GROQ_RATE_STATE",
        "GROQ_FREE_TPM_LIMIT",
    }
)


def _bind_contract(callable_obj, /, *args, label: str, **kwargs) -> None:
    """Validate wrapper-call compatibility without executing provider/network code."""
    try:
        signature = inspect.signature(callable_obj, follow_wrapped=False)
        signature.bind(*args, **kwargs)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"RUNTIME_CALL_CONTRACT_MISMATCH target={label} detail={exc}"
        ) from exc


def _module_runtime_capacity_violations(module) -> list[str]:
    source_path = Path(module.__file__).resolve()
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    violations: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_RUNTIME_CAPACITY_SYMBOLS:
            violations.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_RUNTIME_CAPACITY_SYMBOLS:
            violations.add(node.attr)
    return sorted(violations)


def certify_runtime_patch_contracts() -> dict[str, object]:
    """Certify the final composed runtime surface before production calls providers.

    Run127 proved unit tests for each patch are insufficient when multiple installers
    replace the same callable. This gate checks the FINAL live call signatures after the
    canonical installer order and statically rejects legacy capacity authorities from
    active capacity consumers. It executes no provider request and changes no state.
    """
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

    violations: dict[str, list[str]] = {}
    for module in _CAPACITY_CONSUMER_MODULES:
        found = _module_runtime_capacity_violations(module)
        if found:
            violations[module.__name__] = found
    if violations:
        detail = "; ".join(
            f"{module}:{','.join(symbols)}" for module, symbols in sorted(violations.items())
        )
        raise RuntimeError(
            "RUNTIME_CAPACITY_AUTHORITY_DRIFT "
            f"legacy_symbols_in_active_consumers={detail}"
        )

    result = {
        "status": "pass",
        "signature_checks": len(checks),
        "capacity_consumer_modules": len(_CAPACITY_CONSUMER_MODULES),
        "model_scoped_capacity": True,
        "legacy_capacity_authority": False,
    }
    print(
        "Runtime patch contracts certified: "
        f"signature_checks={result['signature_checks']} "
        f"capacity_consumers={result['capacity_consumer_modules']} "
        "model_scoped_capacity=true legacy_capacity_authority=false"
    )
    return result
