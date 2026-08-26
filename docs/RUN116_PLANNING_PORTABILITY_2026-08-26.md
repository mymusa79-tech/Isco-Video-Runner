# Run 116 Planning Portability Closure

Date: 2026-08-26
Scope: Isco Video Production Resilient V4, approved-film planning path.

## Incident

Run #116 spent about sixteen minutes inside the Production planning step and
failed before plan, voice, visuals, or assembly were produced. The provider
sequence was:

1. Gemini returned HTTP 500 under high demand, including its one bounded retry.
2. Groq was rejected by the local request admission guard because the outline
   prompt was 33,581 UTF-8 bytes while the configured ceiling was 28,672 bytes.
3. OpenRouter returned `finish_reason=length`, so its JSON response was
   incomplete and correctly failed closed.

The visible Telegram state stayed at “planning” because no planning artifact
had passed validation; it was not evidence that useful plan generation was
continuing for the entire period.

## Root cause

The approved topic already carried its editorial scope, but the outline prompt
also inherited generic market samples, broad research/history payloads, and a
repeated full persona/policy representation. The resulting request was too
large for a portable free-provider fallback. GPT-OSS reasoning tokens could
then consume part of OpenRouter's completion budget, making truncation more
likely.

Runs #114 and #115 exercised adjacent failures in the same planning/provider
family (429/413, disconnect/413, malformed or partial output). Run #116 exposed
the remaining input-envelope and completion-budget edge. The earlier retry,
strict-schema, compact-repair, and failure-classification protections remain in
place.

## Production pattern applied

- Treat the approved editorial brief as the immutable planning scope.
- Do not append unrelated market samples to an explicitly approved brief.
- Project and cap only soft/untrusted context fields; preserve approved sources,
  hard constraints, and policy fields, and fail closed if those hard fields
  alone cannot fit.
- Build the exact enriched outline prompt locally before Production and reject
  an oversized envelope without a provider call.
- Use a provider-portable outline ceiling of 26 KiB, below the existing Groq
  request admission ceiling.
- Request low reasoning effort only for the editorial-outline GPT-OSS calls, so
  reasoning does not unnecessarily consume the structured-output budget.
- Record model, finish reason, and token counts, but never prompt or response
  bodies.
- Bound Gemini transport time at 90 seconds; keep Runner as the sole retry owner.

These choices follow the providers' documented guidance to reduce an HTTP 413
request, decompose and shorten large prompts, use strict structured output,
separate reasoning and output budgets, and apply only bounded backoff for
transient failures:

- Gemini troubleshooting: https://ai.google.dev/gemini-api/docs/troubleshooting
- Gemini token counting: https://ai.google.dev/gemini-api/docs/tokens
- Google prompt decomposition: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/prompts/break-down-prompts
- Groq errors: https://console.groq.com/docs/errors
- Groq structured outputs: https://console.groq.com/docs/structured-outputs
- Groq reasoning: https://console.groq.com/docs/reasoning
- OpenRouter reasoning parameters: https://openrouter.ai/docs/api_reference/parameters
- OpenRouter structured outputs: https://openrouter.ai/docs/guides/features/structured-outputs
- OpenRouter response healing limits: https://openrouter.ai/docs/guides/features/plugins/response-healing
- Google SRE retry guidance: https://sre.google/sre-book/addressing-cascading-failures/
- AWS retry/backoff guidance: https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/

## Measured result

Using the exact restored state and approved brief from Run #116:

| Measure | Before | After |
|---|---:|---:|
| Enriched outline prompt | 33,581 bytes | 18,975 bytes |
| Reduction | — | 14,606 bytes (43.5%) |
| Portable ceiling | — | 26,624 bytes |
| Headroom | — | 7,649 bytes |

A realistic full-script prompt built from the compacted planning context is
17,849 bytes, confirming that the excess payload is not merely moved to the
next stage.

## Regression scenarios

- Exact #116 chain: Gemini 500 twice, then compact Groq success; OpenRouter is
  not called and no additional retry is created.
- Oversized soft context is deterministically projected and capped.
- Oversized hard approved context fails closed before any provider call.
- Groq and OpenRouter receive low reasoning effort only for the outline
  contract; other planning contracts are unchanged.
- Safe response metadata contains counts and finish state only.
- Production workflow runs the local planning-envelope certification before
  entering Production.

## Invariants

- Zero-cost-only provider policy remains unchanged.
- Run AI cap remains 42; no new inference call was added.
- There is still one retry owner and no retry amplification.
- Quality, factuality, safety, rights, cultural, and duration gates are not
  weakened.
- Production remains an explicit manual dispatch.
