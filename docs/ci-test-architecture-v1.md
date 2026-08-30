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

## T3 exact-SHA certification

The canonical Full Regression owner now binds certification to an exact Runner identity:

- Pull requests certify the exact PR head SHA.
- Pushes to `main` certify the exact resulting `main` SHA.
- Full Regression runs on every push to `main`; path filters may not leave a merge SHA uncertified.
- The receipt binds Runner SHA, Engine SHA, Engine `requirements-lock.txt` SHA-256, and the canonical Full Regression workflow SHA-256.
- A successful `main` certification publishes `full-regression-green-<runner_sha>` and verifies that any pre-existing tag points to that exact commit.
- The machine-readable receipt records Full Engine, Full Runner, dependency audit, and Approved-Brief CLI matrix as Green and explicitly records that no Production dispatch was performed.

Stage Ladder remains independent evidence and continues to publish `stage-ladder-green-<runner_sha>` on successful `main` certification.

T3 deliberately does not make Production consume these refs yet. The two independent exact-SHA evidences must exist first, and P0 `main` protection must be active before Production Fast Path can be enabled.

## Production status during T3

`produce-resilient-v4.yml` is intentionally unchanged through T3. It still executes Full Engine, Full Runner, and dependency audit before provider work.

Production may use a fast path only after the later isolated stage verifies both exact-SHA certification refs and retains all mutable/live preflights.

## P0 blocker

Before enabling the production fast path, `main` must require pull requests and the canonical certification checks, block force pushes/deletion, and avoid ordinary bypass of those checks.

## Non-goals

T3 does not:

- delete semantic, security, historical-regression, QC, Shorts, or Stage Ladder tests;
- change any Quality Gate threshold;
- change provider/retry/cache semantics;
- launch a Production Run;
- enable production reuse of CI results.
