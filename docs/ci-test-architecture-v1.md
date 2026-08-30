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

`verify-production-stage-ladder.yml` remains the end-to-end P0-P6 certification owner.

## T3 exact-SHA certification

The canonical Full Regression owner binds certification to an exact Runner identity:

- Pull requests certify the exact PR head SHA.
- Pushes to `main` certify the exact resulting `main` SHA.
- Full Regression runs on every push to `main`; path filters may not leave a merge SHA uncertified.
- The receipt binds Runner SHA, Engine SHA, Engine `requirements-lock.txt` SHA-256, and the canonical Full Regression workflow SHA-256.
- A successful `main` certification publishes `full-regression-green-<runner_sha>` and verifies that any pre-existing tag points to that exact commit.
- The machine-readable receipt records Full Engine, Full Runner, dependency audit, and Approved-Brief CLI matrix as Green and explicitly records that no Production dispatch was performed.

Stage Ladder remains independent evidence and continues to publish `stage-ladder-green-<runner_sha>` on successful `main` certification.

## T4 Production certification fast path

`produce-resilient-v4.yml` consumes immutable-code certification instead of rerunning the full immutable regression suites on every video.

Before any private Engine checkout or provider work, `scripts/production_certification_gate.py` fails closed unless all of the following are true:

- the dispatch is from `refs/heads/main`;
- the dispatched SHA is the current `main` SHA;
- GitHub reports `main` as protected;
- `full-regression-green-<runner_sha>` exists and points directly to that exact Runner commit;
- `stage-ladder-green-<runner_sha>` exists and points directly to that exact Runner commit.

Only after this gate is Green may Production continue.

T4 removes the duplicate Full Engine and Full Runner executions from the Production critical path. It intentionally keeps the live dependency vulnerability audit and all mutable/runtime checks, including locked dependency installation, supply-chain/voice validation, memory restore health, Piper preflight, release namespace checks, provider readiness, planning envelope validation, runtime Quality Gates, Final Master QC, Gold, packaging, release transaction, and state persistence.

The certification gate writes `production-certification-gate.json`, which is included in failure diagnostics.

## P0 blocker

T4 is deliberately fail-closed while `main` is unprotected. Production Fast Path cannot be used until repository configuration requires pull requests and canonical certification checks, blocks force pushes/deletion, and avoids ordinary bypass of required checks.

Issue #415 tracks this configuration prerequisite.

## Non-goals

T4 does not:

- delete semantic, security, historical-regression, QC, Shorts, or Stage Ladder tests;
- change any Quality Gate threshold;
- remove the live dependency vulnerability audit;
- change provider/retry/cache semantics;
- launch a Production Run;
- bypass an uncertified or unprotected `main` SHA.
