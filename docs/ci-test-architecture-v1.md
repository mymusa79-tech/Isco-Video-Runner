# CI & Test Architecture Rationalization V1

## Goal

Reduce redundant immutable-code verification without lowering any production Quality Gate.

The design rule is:

- Immutable facts are certified once for an exact identity.
- Mutable facts are verified on every production run.

## T2 ownership

`verify-private-engine.yml` is the canonical CI owner for:

- Full Engine suite.
- Full Runner regression suite.
- Generic dependency vulnerability audit.
- Approved-Brief CLI matrix.

The following workflows are specialized evidence owners and must not rerun the generic full suites:

- `verify-human-editorial-intent-m7.yml`: Human Editorial Intent / M7 focused contracts.
- `verify-m11-live-integration.yml`: M11 focused contracts plus real FFmpeg renderer smoke.
- `verify-voice-identity-observer-v1.yml`: Voice/Gold contracts, immutable reference provenance, and real ECAPA smoke.

`verify-production-stage-ladder.yml` remains the end-to-end P0-P6 certification owner and is not weakened by T2.

## T3 exact-SHA canonical regression receipt

The canonical full-regression owner must certify the exact Runner/Engine identity rather than an implicit pull-request merge checkout.

T3 therefore requires `verify-private-engine.yml` to:

- resolve `CANDIDATE_SHA` as the pull-request head SHA for PR events and `github.sha` otherwise;
- checkout that exact Runner SHA and verify `git rev-parse HEAD` before any certification work;
- resolve the exact canonical Engine SHA from the production workflow and verify the private Engine checkout;
- run on every push to `main`, not a path-filtered subset, so every future main commit can obtain canonical immutable-code certification;
- build a fail-closed `isco.ci.canonical-full-regression-receipt.v1` only after dependency audit, focused regressions, Full Engine, Approved-Brief CLI, Standalone Short V2, Full Runner, and exact closure delta have all passed;
- bind the receipt to both exact SHAs and record `production_dispatch_performed=false`;
- upload the JSON receipt as CI evidence;
- on a successful push to `main`, publish a durable ref named `canonical-full-regression-green-<runner_sha>-<engine_sha>` pointing to that exact Runner commit.

The receipt contract lives in `scripts/exact_sha_regression_receipt.py`. It rejects malformed SHAs, identity mismatches, missing/extra evidence, any non-green evidence, an unexpected owner/schema, a production dispatch marker, or an incorrect certification tag.

The Stage Ladder remains independent and continues to publish its own `stage-ladder-green-<runner_sha>` certification only after P0-P6 pass.

## Production status during T2/T3

`produce-resilient-v4.yml` remains unchanged. It still executes Full Engine, Full Runner, and dependency audit before provider work.

T3 creates certification evidence but **does not enable Production reuse**. Production may use a fast path only after a later isolated stage verifies the exact canonical regression receipt plus Stage Ladder certification and P0 main protection is active.

## P0 blocker

Before enabling the production fast path, `main` must require pull requests and the canonical certification checks, block force pushes/deletion, and avoid ordinary bypass of those checks.

Until that repository configuration exists, absence of a Production fast path is intentional fail-closed behavior.

## Non-goals

T2/T3 do not:

- delete semantic, security, historical-regression, QC, Shorts, or Stage Ladder tests;
- change any Quality Gate threshold;
- change provider/retry/cache semantics;
- launch a Production Run;
- enable production reuse of CI results;
- weaken Stage Ladder certification.
