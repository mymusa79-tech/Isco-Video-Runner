# Production Closure Register

This register records systemic production bug families that must be closed by architecture and executable prevention, not by restoring files, adding one-off retries, or hiding symptoms after a run.

## Closure 1 — Provider guidance / Retry-After ownership

**State:** CLOSED before Run129.

**Failure family:** local latency/retry policy could shorten provider-mandated `Retry-After` evidence and retry the same provider/model before the provider-declared safe window.

**Systemic closure:**
- Provider `Retry-After` is a minimum safe delay, never a value to truncate.
- If the local latency budget can afford the provider delay, wait the full provider delay.
- If it cannot, fail over immediately rather than partially sleeping and retrying early.
- Groq short-window TPM exhaustion is model-scoped; hard daily/session quota remains separate.
- Runtime composition certification rejects reintroduction of partial `Retry-After` ownership.
- Research uses the same canonical rule, preventing the family from surviving outside Planning.

**Acceptance principle:** one retry owner, provider evidence is authoritative, no partial same-provider retry.

## Closure 2 — Test / production source isolation

**State:** BLOCKING CLOSURE. No new production attempt until the PR carrying this closure satisfies the exact-SHA CI criteria below and is merged.

**Failure family:** GitHub Actions test and production steps share one workspace. A successful test suite can mutate tracked Engine state or production inputs and silently change what later production/checkpoint code observes. Run129 exposed this family through tracked Engine state leakage and approved-brief identity drift.

**Systemic closure:**
- Production-approved brief identity comes from the exact pinned Engine commit object, not mutable working-tree bytes.
- A read-only approved-brief snapshot is the canonical runtime/checkpoint input.
- Every Engine/Runner test phase that can touch history receives its own `ISCO_HISTORY_PATH` under runner temporary storage, never the tracked Engine `state/history.json` default.
- Engine tracked-source hermeticity gates fail closed on any tracked modification after test phases.
- Production V4 runs the same isolation and must certify a clean Engine source tree before provider work begins.
- No `git reset`, checkout cleanup, tracked-file allowlist, or post-test restoration is accepted as closure because those approaches hide the write instead of preventing it.

**Exact-SHA acceptance gate:**
1. Full Engine suite: GREEN.
2. Full Runner regression: GREEN.
3. Verify Human Editorial Intent M7: GREEN.
4. Verify M11 Live Integration: GREEN.
5. All four results must target the exact same final PR head SHA.
6. Any code/workflow commit after those results invalidates the evidence and requires all four gates again.

**Production rule:** this closure is a prerequisite for the next production attempt.
