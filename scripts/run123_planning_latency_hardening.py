from __future__ import annotations

import json
import re
import time

import isco_video_agent.resilient_planner as staged
from isco_video_agent.config import load_channel_persona

from scripts import provider_capacity_hardening as capacity
from scripts import run120_dossier_repair_hardening as dossier
from scripts import task_level_planner_router as router


# Run #123 proved that the remaining long-form bottleneck is no longer section count
# alone. Writer/Doctor/Dossier shards repeatedly carried almost the full policy,
# research and persona envelope and were all classified as the same 2,400-token
# ``full_script`` contract. A one-section dossier repair therefore reserved as much
# output as a multi-section writer call and could still reach finish_reason=length on
# OpenRouter. The same near-8K request estimate forced Groq to pace almost every call.
#
# This patch changes transport shape only. It does NOT loosen any Engine quality gate,
# factuality rule, tone rule, section-length gate, Vision/TTS gate, rights rule, Gold
# gate or the Film run-wide provider-attempt hard cap.

_REPAIR_RETRY_AFTER_CAP_SECONDS = 20.0

_SHARD_COMPLETION_BUDGETS = {
    "script_writer_1": 900,
    "script_writer_2": 1300,
    "script_writer_3": 1800,
    "script_doctor_1": 900,
    "script_doctor_2": 1400,
    "script_doctor_3": 1800,
    "dossier_repair_1": 850,
    "dossier_repair_2": 1400,
}

_WRITER_DOCTOR_CONTRACTS = frozenset(
    name for name in _SHARD_COMPLETION_BUDGETS if name.startswith(("script_writer_", "script_doctor_"))
)
_DOSSIER_CONTRACTS = frozenset(
    name for name in _SHARD_COMPLETION_BUDGETS if name.startswith("dossier_repair_")
)
_SHARD_LOW_REASONING_CONTRACTS = frozenset(_SHARD_COMPLETION_BUDGETS)

_TEXT_POLICY_KEYS = (
    "version",
    "audience",
    "positioning",
    "language",
    "values",
    "brand_signature",
    "release_gate",
)
_RESEARCH_KEYS = (
    "approved_research_pack",
    "approved_audience",
    "approved_editorial_direction",
    "content_boundaries",
    "factuality_rule",
)


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_object(raw: str) -> dict:
    value = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise RuntimeError("Run123 transport context must be a JSON object")
    return value


def compact_text_policy_json(raw: str) -> str:
    """Keep every policy field that can affect narration; omit media-only rules.

    Visual and audio policy remain enforced later by their dedicated hard gates. They
    do not need to be repeated inside every narration-writing provider request.
    """
    source = _json_object(raw)
    payload = {key: source[key] for key in _TEXT_POLICY_KEYS if key in source}
    return _compact_json(payload)


def compact_planning_research_json(raw: str) -> str:
    """Preserve factual claim scopes while removing transport-only source URLs.

    Providers cannot browse these URLs during planning. Source validation already
    happened before production; factuality re-audits still receive the original Engine
    research context. The writer only needs the approved source title + exact claim
    scope and the immutable content boundaries.
    """
    source = _json_object(raw)
    payload: dict = {}
    for key in _RESEARCH_KEYS:
        if key not in source:
            continue
        if key != "approved_research_pack":
            payload[key] = source[key]
            continue
        compact_pack = []
        pack = source.get(key)
        if isinstance(pack, list):
            for item in pack:
                if not isinstance(item, dict):
                    continue
                compact_pack.append(
                    {
                        "source_title": str(item.get("source_title") or "").strip(),
                        "claim_scope": str(item.get("claim_scope") or "").strip(),
                    }
                )
        payload[key] = compact_pack
    return _compact_json(payload)


def _contract_name_for_prompt(prompt: str, base_name: str) -> str:
    if base_name != "full_script":
        return base_name
    exact_match = re.search(r"with EXACTLY\s*(\d+)\s+entries", prompt, flags=re.I)
    if not exact_match:
        return base_name
    count = int(exact_match.group(1))
    if "Repair ONLY this bounded shard" in prompt:
        return f"dossier_repair_{count}"
    if "Repair ONE BOUNDED BATCH" in prompt:
        return f"script_doctor_{count}"
    if "writing ONE BOUNDED BATCH" in prompt:
        return f"script_writer_{count}"
    return base_name


def _compact_repair_persona(prompt: str) -> str:
    if "<CHANNEL_PERSONA>" in prompt or "نداء اليقظة" not in prompt:
        return prompt
    persona = load_channel_persona()
    writing = persona["writing_voice"]
    lens = persona["analysis_lens"]
    compact = {
        "version": persona["version"],
        "channel": persona["channel"],
        "scope": "bounded_script_repair",
        "writing_voice": {
            "tone": writing["tone"],
            "cadence": writing["cadence"],
            "signature_moves": writing["signature_moves"],
            "banned_ai_phrases": writing["banned_ai_phrases"],
        },
        "analysis_lens": {
            "principle": lens["principle"],
            "required_moves": lens["required_moves"],
            "generic_rejection_rule": lens["generic_rejection_rule"],
        },
    }
    if "dialogue_qa" in prompt:
        dialogue = persona["dialogue_contract"]
        compact["dialogue_contract"] = {
            "rule": dialogue["rule"],
            "question_answer_rule": dialogue.get("question_answer_rule", ""),
        }
    return (
        prompt
        + "\n\n<CHANNEL_PERSONA>\n"
        + _compact_json(compact)
        + "\n</CHANNEL_PERSONA>\n"
        + "CHANNEL_PERSONA is mandatory editorial identity. Preserve its tone, cadence, signature moves, banned-phrase rules and analysis lens."
    )


def _fast_failover_groq_pacing(request_capacity: dict) -> float:
    """Use another free provider instead of sleeping on a known-empty Groq TPM window.

    No HTTP request and therefore no BudgetLedger provider attempt happens here. The
    router will advance to OpenRouter for this logical subtask. Groq is NOT circuit
    opened: once the recorded reset time passes, its local state is cleared and it is
    eligible again on a later subtask.
    """
    remaining = capacity._GROQ_RATE_STATE.get("remaining_tokens")
    reset_at = capacity._GROQ_RATE_STATE.get("reset_at_monotonic")
    required = int(request_capacity["estimated_request_tokens"])
    now = time.monotonic()

    if isinstance(reset_at, (int, float)) and float(reset_at) <= now:
        capacity._GROQ_RATE_STATE["remaining_tokens"] = None
        capacity._GROQ_RATE_STATE["reset_at_monotonic"] = None
        return 0.0

    if isinstance(remaining, int) and required > remaining and isinstance(reset_at, (int, float)):
        until_reset = max(0.0, float(reset_at) - now)
        raise RuntimeError(
            "GROQ_TPM_WINDOW_BUSY_PRECHECK "
            f"required_estimate={required} remaining={remaining} reset_in={until_reset:.2f}s "
            "action=failover_without_http"
        )
    return 0.0


def install_run123_planning_latency_hardening() -> None:
    if getattr(router, "_ISCO_RUN123_PLANNING_LATENCY_HARDENED", False):
        return

    original_schema = router._structured_schema_for_prompt
    original_budget = capacity.completion_token_budget
    original_persona = router.with_channel_persona
    original_write = staged._write_full_script
    original_doctor = staged._script_doctor
    original_dossier_prompt = dossier._repair_prompt
    original_response_format = capacity._response_format_for_contract

    def shard_schema(prompt: str):
        contract = original_schema(prompt)
        if contract is None:
            return None
        name, schema = contract
        return _contract_name_for_prompt(prompt, name), schema

    def shard_completion_budget(contract) -> int:
        name = contract[0] if contract else "json_object"
        return _SHARD_COMPLETION_BUDGETS.get(name, original_budget(contract))

    def shard_response_format(contract):
        if contract is not None and contract[0] in _DOSSIER_CONTRACTS:
            schema_name, schema = contract
            return {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
        return original_response_format(contract)

    def task_persona(prompt: str) -> str:
        if "Repair ONE BOUNDED BATCH" in prompt or "Repair ONLY this bounded shard" in prompt:
            return _compact_repair_persona(prompt)
        return original_persona(prompt)

    def compact_writer(*args, **kwargs):
        if "policy_json" in kwargs:
            kwargs["policy_json"] = compact_text_policy_json(kwargs["policy_json"])
        if "research_json" in kwargs:
            kwargs["research_json"] = compact_planning_research_json(kwargs["research_json"])
        return original_write(*args, **kwargs)

    def compact_doctor(*args, **kwargs):
        if "policy_json" in kwargs:
            kwargs["policy_json"] = compact_text_policy_json(kwargs["policy_json"])
        if "research_json" in kwargs:
            kwargs["research_json"] = compact_planning_research_json(kwargs["research_json"])
        return original_doctor(*args, **kwargs)

    def compact_dossier_prompt(*args, **kwargs):
        if "policy_json" in kwargs:
            kwargs["policy_json"] = compact_text_policy_json(kwargs["policy_json"])
        if "research_json" in kwargs:
            kwargs["research_json"] = compact_planning_research_json(kwargs["research_json"])
        return original_dossier_prompt(*args, **kwargs)

    router._structured_schema_for_prompt = shard_schema
    router.with_channel_persona = task_persona
    capacity.completion_token_budget = shard_completion_budget
    router._completion_tokens_for_contract = shard_completion_budget

    # Reuse the capacity layer's existing low-reasoning branch and robust OpenRouter
    # fallback family for every bounded script shard. Dossier repairs still keep strict
    # JSON Schema through shard_response_format(), so low reasoning does not trade away
    # structural guarantees.
    capacity._OUTPUT_HEAVY_CONTRACTS = frozenset(
        set(capacity._OUTPUT_HEAVY_CONTRACTS).union(_SHARD_LOW_REASONING_CONTRACTS)
    )
    capacity._response_format_for_contract = shard_response_format

    # Do not serialize the whole planning pipeline behind Groq's minute window. A
    # known-insufficient remaining-token header is a local routing fact, not a reason
    # to sleep. OpenRouter can handle that subtask while Groq naturally becomes
    # eligible again after reset.
    capacity._proactive_groq_pacing = _fast_failover_groq_pacing

    # An actual escaped HTTP 429 may still include Retry-After. Keep one bounded outer
    # retry, but never allow the old 120-second wait to dominate the production path.
    router.RETRY_AFTER_MAX_SECONDS = min(
        float(router.RETRY_AFTER_MAX_SECONDS), _REPAIR_RETRY_AFTER_CAP_SECONDS
    )

    staged._write_full_script = compact_writer
    staged._script_doctor = compact_doctor
    dossier._repair_prompt = compact_dossier_prompt

    # Run #123 exposed the 120-minute workflow margin as an end-to-end concern, not
    # only a planner concern. Keep media quality unchanged but make ffmpeg/ffprobe
    # subprocesses finite so a later hung render cannot consume the entire job.
    from scripts.run123_runtime_latency_guard import install_run123_runtime_latency_guard

    install_run123_runtime_latency_guard()
    router._ISCO_RUN123_PLANNING_LATENCY_HARDENED = True
    print(
        "Run123 planning latency hardening installed: "
        "writer_doctor=dynamically_bounded dossier=strict_schema_low_reasoning "
        "groq_window=failover_without_sleep repair_persona=compact "
        "factual_claim_scopes=preserved retry_after_cap=20s"
    )
