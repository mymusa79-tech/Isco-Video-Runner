from __future__ import annotations

import ast
import inspect
import textwrap

import isco_video_agent.director_eyes as director_eyes
import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.providers import gemini as gemini_provider


class ProviderRetryOwnershipError(RuntimeError):
    pass


def _tree(fn) -> ast.AST:
    try:
        source = textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError) as exc:
        raise ProviderRetryOwnershipError(
            f"provider_retry_contract_source_unavailable target={getattr(fn, '__name__', type(fn).__name__)}"
        ) from exc
    return ast.parse(source)


def _callable_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _literal_attempts_one_calls(fn, target_name: str) -> int:
    """Count single-attempt provider handoffs, direct or callback-mediated.

    Engine's direct-provider ledger intentionally receives the provider function as a
    positional callback (`_ledger_call(..., synthesize_wav, ..., attempts=1)`). That is
    the real wire boundary even though AST does not represent it as `synthesize_wav()`.
    Treat both shapes equivalently while still requiring a literal attempts=1 keyword.
    """
    count = 0
    for node in ast.walk(_tree(fn)):
        if not isinstance(node, ast.Call):
            continue
        direct = _callable_name(node.func) == target_name
        callback = any(_callable_name(argument) == target_name for argument in node.args)
        if not (direct or callback):
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "attempts"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == 1
            ):
                count += 1
    return count


def _assert_single_wire_call_without_loop(fn, *, label: str) -> None:
    tree = _tree(fn)
    loops = [node for node in ast.walk(tree) if isinstance(node, (ast.For, ast.AsyncFor, ast.While))]
    if loops:
        raise ProviderRetryOwnershipError(
            f"provider_retry_loop_detected target={label} loops={len(loops)}"
        )

    wire_calls = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "create":
            continue
        value = node.func.value
        if isinstance(value, ast.Attribute) and value.attr == "interactions":
            wire_calls += 1
    if wire_calls != 1:
        raise ProviderRetryOwnershipError(
            f"provider_wire_call_count_mismatch target={label} expected=1 actual={wire_calls}"
        )


def certify_provider_retry_ownership() -> dict[str, object]:
    """Fail before production if TTS/Vision acquire hidden same-provider retry loops.

    PR #363's Retry-After scan protects one source pattern in Runner. This contract
    covers the broader ownership invariant at the live provider boundaries where a
    retry loop could ignore provider evidence without containing Retry-After at all.
    It performs no network/provider call.
    """
    final_tts = orchestrator.synthesize_wav
    signature = inspect.signature(final_tts, follow_wrapped=False)
    attempts = signature.parameters.get("attempts")
    if attempts is None or attempts.default != 1:
        raise ProviderRetryOwnershipError(
            "tts_retry_owner_drift final_voice_boundary_attempts_default_must_equal_1"
        )

    # The Runner Voice Mesh must pass a literal one to Engine's historical provider
    # adapter, never forward a caller-controlled retry count.
    if _literal_attempts_one_calls(final_tts, "gemini_synthesize") != 1:
        raise ProviderRetryOwnershipError(
            "tts_retry_owner_drift voice_mesh_must_forward_attempts_1_exactly_once"
        )

    # Engine's production TTS owner passes synthesize_wav as a callback into its direct
    # provider ledger and must force attempts=1 when Runner's Piper fallback is installed.
    # TtsBudget/TtsCircuit then owns the one optional bonus cloud attempt and failover.
    if _literal_attempts_one_calls(orchestrator._synthesize_tts_section, "synthesize_wav") < 1:
        raise ProviderRetryOwnershipError(
            "tts_retry_owner_drift engine_runner_path_missing_attempts_1"
        )

    # Current production Vision boundaries are one wire request per distinct review.
    # Candidate iteration is editorial selection of different assets, not a retry of
    # the same provider request, so it remains outside this invariant.
    vision_boundaries = (
        (gemini_provider.audit_video_preview, "gemini.audit_video_preview"),
        (gemini_provider.audit_image_preview, "gemini.audit_image_preview"),
        (director_eyes.judge_candidate_reel, "director_eyes.judge_candidate_reel"),
    )
    for fn, label in vision_boundaries:
        _assert_single_wire_call_without_loop(fn, label=label)

    result = {
        "status": "pass",
        "tts_final_default_attempts": 1,
        "tts_outer_retry_owner": "engine_tts_budget_circuit",
        "vision_single_wire_boundaries": len(vision_boundaries),
        "provider_calls_executed": 0,
    }
    print(
        "Provider retry ownership certified: "
        "tts_single_attempt_boundary=true tts_outer_owner=engine_tts_budget_circuit "
        f"vision_single_wire_boundaries={len(vision_boundaries)} provider_calls=0"
    )
    return result
