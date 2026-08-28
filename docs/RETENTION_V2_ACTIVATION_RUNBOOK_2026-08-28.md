# Retention Intelligence V2 — Deferred Activation Runbook

Date: 2026-08-28
Activation state: prepared, intentionally not live-bound.
Activation prerequisite: Production stability after Run126.

## Objective

Connect Retention Intelligence V2 as a **separate post-publication learning workflow**, not as part of the render/Gold/release critical path.

The approved sequence is:

`retention evidence -> signals (M3) -> cohorts (M4) -> reach context (M5) -> experiment registry (M6) -> reports`

M7 influence remains recommendation/candidate-only and fail-closed.

## Why activation is deferred

The current production job can collect observational YouTube metrics, but its persistent state closure is centered on encrypted `history.json`. Raw retention sidecars and downstream M2-M6 evidence need an explicit durable cross-run storage contract before cohort learning can be considered live. Wiring M3-M6 before that durability exists would create a misleading "live" status while evidence disappears with ephemeral runner storage.

## Future activation requirements

### 1. Separate execution boundary

Use a dedicated post-publication workflow or equivalent observer job. Do not import Retention V2 into `scripts/run_v3_voice.py`, and do not add it between rendering, Final Master QC, Gold enforcement, manifest creation, or release.

The job must be safe to run after publication data becomes available and safe to rerun idempotently.

### 2. Verified provenance

For agent-produced videos, retain an explicit, reviewed mapping of:

- YouTube `video_id`
- production id
- production binding source
- production artifact/timeline identity when available

Unverified channel observations must remain isolated from verified agent-produced evidence.

### 3. Durable retention evidence

Persist raw M1 retention observations and M2 bindings across runs using a reviewed authenticated storage mechanism. Do not rely on `$RUNNER_TEMP` as the only copy.

The persistence design must preserve evidence immutability/deduplication and must not weaken the existing authenticated state boundary.

### 4. M3-M6 integration

After durable M1/M2 evidence exists, execute and report:

1. M3 deterministic signal extraction.
2. M4 cohort aggregation only when evidence and minimum cohort requirements pass.
3. M5 read-only reach context; no Reporting API job mutation and no causal inference across reach/retention.
4. M6 experiment registry with external/manual assignment and verified outcomes only.
5. Human-readable reports/artifacts that distinguish insufficient evidence from meaningful evidence.

Missing credentials, missing reach reports, sparse cohorts, or incomplete experiments must fail soft at the observer layer and must never fail video production.

### 5. M7 lock

Initial activation must keep `ISCO_RETENTION_INFLUENCE_MODE` at `disabled` or `review_only`.

Do not enable `candidate` mode until there is a separately reviewed body of real evidence satisfying the M7 sample, effect, approval and independent statistical validation gates. Even then, eligible output remains a candidate requiring explicit manual integration; automatic application remains forbidden.

## Required activation tests

Before changing status from deferred to live-observing, add an integration test that proves:

- the post-publication path invokes M3 -> M4 -> M5 -> M6 in the approved order when evidence permits;
- the path is absent from the production entrypoint;
- missing YouTube credentials/data cannot fail production;
- verified and unverified evidence cannot cross contamination boundaries;
- raw evidence survives a simulated cross-run restore;
- repeated observation is idempotent/deduplicated where contracts require it;
- M7 cannot automatically mutate production configuration.

## Status vocabulary

Until all activation requirements pass, use:

**implemented + tested + activation prepared + isolated from production + deferred until post-Run126 stability and durable evidence storage**

Do not label Retention V2 simply `live` or `ready`.
