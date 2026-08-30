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

## Production status during T2

`produce-resilient-v4.yml` is intentionally unchanged in T2. It still executes Full Engine, Full Runner, and dependency audit before provider work.

Production may use a fast path only after a later isolated stage provides a fail-closed exact-SHA certification receipt and P0 main protection is active.

## P0 blocker

Before enabling the production fast path, `main` must require pull requests and the canonical certification checks, block force pushes/deletion, and avoid ordinary bypass of those checks.

## Non-goals

T2 does not:

- delete semantic, security, historical-regression, QC, Shorts, or Stage Ladder tests;
- change any Quality Gate threshold;
- change provider/retry/cache semantics;
- launch a Production Run;
- enable production reuse of CI results.
