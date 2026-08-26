# Planning / Provider Reliability V2 — Run 115 Closure

Date: 2026-08-26
Scope: Isco Video Production Resilient V4 planning stage only.

## Incident classification

Run #115 progressed through checkout, Engine pin verification, approved brief validation, full Runner regression, dependency audit, encrypted-memory restore, local voice preflight, environment/release namespace preflight, secret materialization and provider readiness. It failed after entering Production planning.

The terminal provider chain was:

1. Gemini: `Server disconnected without sending a response.`
2. Groq GPT-OSS 20B: HTTP 413 for the same large planning request.
3. OpenRouter free route: invalid JSON after the response reached the client.

This is the same broad **Planning / Provider Reliability** family as Runs #113–#114, but a new edge. Run #114's compact partial Script Doctor recovery was not reached and is not the failure in #115.

## External production patterns adopted

### Google Gemini

Official Gemini API error guidance distinguishes short-window `rate_limit_exceeded` from `quota_exceeded`, and recommends exponential backoff for retryable 429/503 conditions. Structured Outputs supports JSON Schema and applications must still validate semantic values.

Sources:
- https://ai.google.dev/gemini-api/docs/api-errors
- https://ai.google.dev/gemini-api/docs/troubleshooting
- https://ai.google.dev/gemini-api/docs/structured-output

Applied:
- transport disconnect variants map to `network_error` and receive the one existing bounded outer retry;
- session quota remains run-circuiting;
- known planning tasks use their exact JSON schema through the existing Gemini planning guard;
- incomplete/empty output remains fail-closed.

### Groq

Groq documents HTTP 413 as request-body-too-large and explicitly says to reduce the request. GPT-OSS 20B supports strict Structured Outputs (`json_schema`, `strict:true`) and Groq recommends `json_schema` over legacy `json_object` when supported. Free-plan GPT-OSS 20B currently has bounded token/request rate limits and exposes `retry-after` plus remaining-token/request headers.

Sources:
- https://console.groq.com/docs/errors
- https://console.groq.com/docs/structured-outputs
- https://console.groq.com/docs/api-reference
- https://console.groq.com/docs/rate-limits

Applied:
- large prompts are rejected by local request admission before an HTTP call, then routing falls through;
- 413 remains request-scoped and does not poison Groq for later compact repairs;
- known planning contracts use strict native JSON Schema;
- `finish_reason=length`, empty output and no choices become explicit premature-output failures;
- `retry-after` is honored within a hard 30-second ceiling when present.

### OpenRouter

OpenRouter supports strict structured outputs, provider/model fallback, `require_parameters`, and its free Response Healing plugin for malformed JSON. OpenRouter states Response Healing fixes syntax defects, not schema adherence, and does not replace application-level semantic validation.

Sources:
- https://openrouter.ai/docs/features/structured-outputs
- https://openrouter.ai/docs/features/provider-routing
- https://openrouter.ai/docs/features/model-routing
- https://openrouter.ai/docs/features/plugins/response-healing
- https://openrouter.ai/blog/announcements/response-healing-reduce-json-defects-by-80percent/

Applied:
- known production planning requests use strict schema + parameter-capable routes + provider/model fallback + Response Healing;
- truncation never enters syntax repair;
- if malformed JSON remains after healing, the single existing repair allowance uses only a bounded copy of the malformed output, never a replay of the large original planning prompt;
- all routing remains restricted to the approved free model chain.

### SRE retry architecture

Google SRE and AWS reliability guidance warn that retries at multiple layers amplify load. The production pattern is one retry owner, exponential backoff with jitter, bounded retry counts, failure classification, and circuits/admission control for deterministic failures.

Sources:
- https://sre.google/sre-book/addressing-cascading-failures/
- https://docs.aws.amazon.com/wellarchitected/latest/framework/rel_mitigate_interaction_failure_limit_retries.html
- https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/

Applied:
- Runner remains the single client-side retry owner;
- no unbounded retry was added;
- provider/model routing inside OpenRouter remains one gateway request layer;
- deterministic/request-scoped failures do not trigger blind retries;
- no quality gate or threshold was lowered.

## Defense matrix after V2

| Failure | Scope | Recovery | Circuit |
|---|---|---|---|
| Network disconnect / reset / EOF | transient | original + one bounded retry | no; temporary cooldown after exhaustion |
| 500/502/503/504 | transient | original + one bounded retry | no; temporary cooldown after exhaustion |
| 429 with explicit Retry-After | transient | one bounded retry honoring Retry-After (max 30s) | yes after exhaustion |
| Daily/session quota | provider session | no blind retry | yes |
| 401/403 | provider session | no retry | yes |
| Model/config failure | provider session | no retry | yes |
| Groq 413 / local size admission | request | fail over immediately | no |
| Groq 422 generation error | transient/request | one bounded retry | no |
| `finish_reason=length` / incomplete / empty | request/output | fail closed; no syntax repair | no |
| Malformed JSON | output syntax | native structured output; OpenRouter healing; at most one compact repair | no |
| Schema-semantic mismatch | output schema | existing single schema-repair owner | no provider replay |
| Quality/factuality/safety failure | content | existing strict quality owner | not a provider reliability retry |
| AI budget exhausted | task/run | fail closed | no extra calls |

## Invariants

- Zero-cost provider policy unchanged.
- No paid fallback added.
- Quality, factuality, safety, rights, cultural and duration gates unchanged.
- No unbounded loop or retry amplification added.
- OpenRouter syntax repair may not replay the original large prompt.
- Provider telemetry stores prompt byte count and contract name, never prompt text or secrets.
- Request-specific Groq size failures do not make later compact Groq calls ineligible.
