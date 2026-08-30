# Production Orchestration L0 — Contract Freeze

Status: IMPLEMENTATION BASELINE ONLY — NO MERGE AUTHORITY — NO PRODUCTION RUN AUTHORITY

Baseline Runner `main` SHA: `7b832314bf1902f2cde295ca3236af91fa202446`

Certified Planning merge source: PR #396 head `a9f55253631b79d43ed26145b0c0e5e2adde6095`

Post-merge certification:
- Verify Production Stage Ladder #143: Green on merged `main`.
- Verify Private Engine #1034 (`workflow_dispatch`): Green on merged `main`, including Full Engine suite, Full Runner regression suite, and exact closure sanity.

## Purpose

L0 freezes the architectural boundaries for the Production Orchestration Layer before any runtime implementation. It introduces no production behavior and no competing Planning implementation.

The Orchestration control plane may add canonical journal/reducer state, deadline admission, capability manifests, a general stage registry, Telegram ingress/outbox, and stable ports in later stacked layers. It MUST preserve existing proven Data Plane cores and MUST NOT lower any existing Quality, Safety, Security, trust, provenance, receipt, or reconciliation gate.

## Planning #396 — read-only protected surface

The following nine files are the exact PR #396 changed-file surface and are READ-ONLY for Production Orchestration work unless a later explicit Planning change is separately approved:

1. `scripts/append_retry_guard.py`
2. `scripts/bounded_output_recovery.py`
3. `scripts/planning_legacy_authority_guard.py`
4. `scripts/planning_runtime_contract.py`
5. `scripts/planning_stage_contract.py`
6. `scripts/schema_repair_policy.py`
7. `scripts/test_planning_append_stage_contract.py`
8. `scripts/test_planning_legacy_authority_guard.py`
9. `scripts/test_planning_stage_contract.py`

### Planning non-conflict invariants

- No competing or copied Planning `StageContract` is allowed.
- The general registry MUST accept external/plugin registration so Planning can be registered later without changing registry core semantics.
- L5 MUST consume canonical metadata exported from the merged Planning contract; it MUST NOT infer Planning identity from prompt text, wrapper position, filename, or a duplicated schema.
- Planning `stage_id`, `contract_id`, input identity/hash semantics, output schema, semantic rules, provider policy, cache policy, retry ownership, admission behavior, validation ownership, checkpoint binding, and error taxonomy remain owned by the certified Planning path.
- A Planning cache/checkpoint hit is never authoritative before current-contract verification.
- Planning cache write remains after structural + semantic validation at its single approved commit seam.
- No provider call boundary may acquire a second retry owner through orchestration wrappers.
- Any Planning/registry mismatch MUST fail closed as an internal contract error in the adapter layer; Planning validators are not weakened to make the registry fit.

## Existing cores retained

The following cores remain authoritative and are wrapped only through adapters/ports later:

- TTS durable behavior, Voice Mesh, and Audio Semantic Integrity.
- Media durable behavior, Security V1, Media Trust Boundary V2, provenance/trust validation.
- Cinematic live capability implementations (M8/M9/M10/M11/SFX/CTA).
- Render durable/content-addressed behavior.
- Final Master QC and observer evidence.
- Shorts canonical bundle/child admission/delivery behavior.
- Release transaction, reconciliation, receipt validation, asset verification, and target-SHA authority.

No core above is rewritten merely to enter the Orchestration layer.

## Cross-layer invariants frozen at L0

1. **Single Authority** — stage contract owns stage policy; journal owns canonical operational state; capability manifest owns composition conformance; release transaction owns publication; Telegram ingress owns bot updates.
2. **Validate Before Trust** — restored artifacts, cache hits, registry metadata, approvals, and journal restores are candidates until validated against current identity/schema/bindings.
3. **Idempotency First** — retryable side effects use idempotency/reconciliation, never blind duplicate calls.
4. **Budget Before Work** — no costly stage starts unless remaining budget covers its minimum viable budget plus downstream reserve; child deadlines derive from the parent deadline.
5. **No Silent Skip** — a required capability cannot be silently skipped and still pass release eligibility.
6. **Journal Is Source of Truth** — GitHub/Telegram are projections, not canonical production state.
7. **Adapter Before Rewrite** — existing seams are first wrapped for parity, then exposed through stable ports; old monkey patches are removed only after adapter parity + full gates + exact behavior review.
8. **Compatibility Is Explicit** — legacy behavior that remains must have a documented compatibility contract and drift-prevention tests.
9. **No Quality Gate weakening** — existing Quality/Safety/Security gates remain additive prerequisites.
10. **No automatic Merge / Production Run** — no stacked layer or integrated gate grants merge or production-run authority.

## Stacked isolation baseline

- L0 branch base: `7b832314bf1902f2cde295ca3236af91fa202446`.
- L1 MUST be based on the final L0 head, not independently on `main`.
- L2 MUST stack on L1; L3 on L2; L4 on L3; L5 on L4; L6 on L5; L7 migrations stack in small isolated steps.
- Each layer remains unmerged until separate merge approval.

## L0 acceptance

L0 is complete when:

- the exact #396 protected surface is recorded;
- the merged/certified baseline SHA is recorded;
- non-conflict and ownership boundaries are explicit;
- no runtime code has changed;
- no Quality/Safety/Security/Release authority changed;
- no Production Run or merge was initiated.
