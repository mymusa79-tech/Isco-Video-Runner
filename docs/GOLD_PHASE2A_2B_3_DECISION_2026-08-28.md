# Gold Phase 2A / 2B / 3 — Decision

Date: 2026-08-28
Decision: B — retain and document; do not re-enable as parallel production paths.

## Canonical status

Gold Phase 2A, Phase 2B and Phase 3 are **successfully superseded migration/shadow stages**. They were deliberate steps in the migration toward Phase 4 enforcement; they are not failed quality paths and they are not alternate release authorities.

## Current authority

`run_gold_enforce_phase4` is the sole live Gold enforcement path in current production. The older stages must not be invoked in parallel with Phase 4 because doing so would recreate duplicate evaluation, duplicate provider activity and ambiguous release state.

## Retention policy

Do not delete the Phase 2A implementation at this time. Phase 4 currently imports helper utilities from `scripts.gold_shadow_phase2a`, including `_fingerprint` and `_provider_attempt_total`. Deleting or moving that file without first refactoring those live dependencies would break the Phase 4 runtime contract.

Phase 2B and Phase 3 are retained as migration/regression/history references. Their presence does not mean they are live.

## Future cleanup rule

A future cleanup may extract the Phase 2A helper utilities into a neutral shared module and then reconsider whether the superseded migration files still provide enough regression/history value to retain. Such cleanup must be a separate reviewed change with tests proving Phase 4 behavior is unchanged.

Until that happens:

- Phase 4 = live and authoritative.
- Phase 2A/2B/3 = superseded migration/shadow references.
- No parallel activation.
- No deletion of Phase 2A utilities.
