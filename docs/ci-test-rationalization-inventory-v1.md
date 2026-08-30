# CI/Test Rationalization Inventory V1

This inventory records ownership decisions discovered during T5. The goal is to reduce brittle implementation snapshots without losing fault coverage.

## Decision rule

- **KEEP**: behavioral, security, contract, semantic, or real integration evidence with distinct fault ownership.
- **REWRITE**: the protected invariant is valid but the assertion is coupled to source text, line order, or superseded installer topology.
- **MERGE**: another current owner proves the same invariant more directly; strengthen that owner before retiring the duplicate.
- **RETIRE**: only after replacement ownership is explicit and the canonical Full Runner plus Stage Ladder remain Green.

## Reviewed families

### `scripts/test_runtime_closure.py` — REWRITE/MERGE completed for one duplicate

Permanent behavioral ownership is retained for:
- planning seam before media/reliability composition;
- Media → core reliability → Audio Semantic binding → mastering → Cinematic INNER → Render order;
- Render → music → bundle → release → telemetry → semantic final gate order;
- observer cache trust before observer durability;
- post-Gold observer fail-open/observe-only behavior and secret-file handling;
- canonical bundle activation constraints.

The former AST/source-line test for stable-port ordering duplicated the behavioral call-order test. T5 strengthens the behavioral test to assert the complete owned sequence and retires only that duplicate topology snapshot.

### `scripts/test_run130_explicit_runtime_phase.py` — KEEP, selective REWRITE candidate

KEEP:
- workflow identity alone must not activate runtime;
- explicit runtime activation still requires canonical workflow identity;
- activation must update both current process and later GitHub Actions steps;
- immutable snapshot and durable persistence must remain inactive during pre-production and active only in live runtime.

Candidate for later REWRITE, not deletion:
- helper lists that enumerate historical planning/non-planning installer names;
- source-text ordering assertion in persistent-memory phase transition.

These are not retired in T5 Batch 1 because their replacement behavioral owner has not yet been proven independently.

### `scripts/test_p0c_migration_contracts.py` — KEEP with future decomposition

KEEP as distinct high-value contracts:
- one Runner ledger forwarded to core and Gold authority;
- Gold Phase 4 enforcement/state-transition ownership;
- Phase 3 observe-only/state immutability;
- verified-manifest analytics binding;
- voice mesh cloud/local patch ownership;
- planner router Engine patch contract;
- single Pexels secret materialization/reuse contract;
- thumbnail budget delegation boundary.

Future REWRITE candidates are the broad source-string/topology freeze assertions around installer presence and ordering. They are not deleted until L7/stage-registry or behavioral owners are explicitly mapped to each invariant.

### `scripts/test_run129_production_test_isolation_contract.py` — migration completed in T4

The historical version required Production itself to own Full Engine and Full Runner test-state paths. After canonical Full Regression ownership and exact-SHA certification, those assertions became stale topology requirements. They were rewritten to forbid duplicate Production full-suite execution and require the fail-closed exact-SHA certification gate instead. All five certification workflows are Green on the migrated exact head.

## Explicit non-candidates for blanket removal

- `scripts/test_l7_integrated_gate.py`
- Final Master QC semantic/FFmpeg tests
- Shorts production binding/template behavior tests
- Stage Ladder P0–P6 evidence
- Security/supply-chain/provenance/fail-closed contracts

These are semantic or integration owners, not cleanup targets merely because they increase test count.

## T5 stop condition

No legacy assertion is retired unless:
1. the protected invariant is named;
2. a current behavioral/contract owner is identified or strengthened;
3. the exact T5 head passes canonical Full Engine + Full Runner ownership and Production Stage Ladder;
4. specialized M7/M11/Voice gates remain Green;
5. runtime and Quality Gate thresholds remain unchanged.
