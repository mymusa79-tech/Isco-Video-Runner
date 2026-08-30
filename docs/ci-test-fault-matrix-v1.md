# CI/Test Fault Ownership Matrix V1

## Purpose

This matrix closes the CI/Test Architecture Rationalization review by assigning each protected failure class to one authoritative evidence owner. It does not add topology/meta-tests and it does not lower or remove any Quality Gate.

Design rule:

- **Immutable code/contract facts** are certified once for an exact Runner/Engine identity.
- **Mutable runtime facts** are verified at runtime or on every Production run where they can change.
- **External Telegram/device facts** require external smoke evidence and are never implied by an immutable CI receipt.

The canonical Full Regression owner is `.github/workflows/verify-private-engine.yml`. Its Full Runner step dynamically discovers every top-level `scripts/test_*.py`, so new Telegram/system regression tests following that repository contract are included automatically rather than through a hand-maintained allow-list.

## Fault matrix

| Fault class | Authoritative owner / evidence | Layer | Required behavior |
|---|---|---|---|
| Uncertified Runner SHA | `scripts/test_production_certification_gate.py`, `scripts/test_ci_exact_sha_certification.py`, canonical Full Regression receipt/tag | Certification + Production preflight | Fail closed before provider or production work. |
| Runner is not current protected `main` | `scripts/production_certification_gate.py` and its tests | Production preflight | Reject stale/non-main/unprotected identity. |
| Engine pin mismatch / non-canonical Engine identity | `verify-private-engine.yml` canonical V4 Engine contract + exact checkout verification | Certification | Fail closed before Full Engine/Runner certification. |
| Dependency lock or certification workflow changes | Exact Runner/Engine identity plus receipt hashes for Engine `requirements-lock.txt` and canonical Full Regression workflow | Certification | Require a new exact-identity certification; old evidence is not transferable. |
| Stage/orchestration contract changes | Exact Runner identity; `scripts/test_orchestration_stage_registry.py`; `scripts/test_l7_integrated_gate.py`; Production Stage Ladder P0-P6 | Certification | Re-certify exact changed Runner SHA and preserve end-to-end stage evidence. |
| Piper cache poisoned/corrupt/symlinked/unexpected contents | `scripts/test_piper_bootstrap_cache.py` + `scripts/piper_bootstrap_cache.py` validation | Bootstrap + runtime preflight | Cache is speed only: reject invalid cache, rebuild/download, hash/manifest verify before use. |
| Live Piper/runtime voice unavailable or unusable | Production Piper/voice preflight and Voice Identity specialized gate | Runtime/live | Fail or use only the explicitly allowed fallback policy; immutable cache evidence cannot replace live readiness. |
| Untrusted or invalid selected media | `scripts/test_media_trust_boundary_v2.py` + live Media Trust Boundary checks | Runtime/live | Reject media that does not satisfy trust/provenance policy. |
| Provider unavailable/capacity exhausted/retry ownership violated | `scripts/test_provider_preflight.py`, `scripts/test_provider_failure.py`, `scripts/test_provider_retry_ownership.py`, capacity/admission regressions + live provider preflight | Certification + runtime/live | Classify and fail/fallback under bounded policy; no unbounded retries. |
| Structurally/semantically invalid Planning output | `scripts/test_planning_stage_contract.py`, `scripts/test_planning_envelope_preflight.py`, Planning quality/schema guards | Certification + runtime/live | Never make invalid output or cache state authoritative. |
| Corrupt/stale/mismatched Planning checkpoint or persisted state | `scripts/test_planning_checkpoint_state.py`, persistent-memory/checkpoint guards + live restore validation | Certification + runtime/live | Reject or rebuild invalid state; never silently trust mutable state. |
| Broken FFmpeg/rendered output | `scripts/test_final_master_qc_ffmpeg.py`, M11 real FFmpeg smoke, live Final Master QC | Integration + runtime/live | Detect malformed/unplayable output before release. |
| Final QC/Gold/content gate failure | Final Master QC, Gold/final critic/content Quality Gates | Runtime/live | Block release; no threshold lowering or certification bypass. |
| Release target/assets/hash transaction mismatch | `scripts/test_release_transaction.py`, release reconciliation contracts, Telegram release identity/approval tests + live release transaction | Certification + runtime/live | Fail closed; release approval remains a deliberately independent narrow seam. |
| Telegram stateful callback falls through to Legacy UI instead of Active UI | `scripts/test_telegram_saved_replay_e2e.py`, `scripts/test_telegram_webhook_replay.py`, Active UI/control regression family | Full Runner system/E2E certification | Edge/Webhook Replay callbacks reaching the common stateful path must arrive at the modern router. |
| Telegram search/saved/used/topic lifecycle regression | `scripts/test_telegram_research_selection_flow.py`, `scripts/test_telegram_library_split.py`, `scripts/test_telegram_topic_memory_ui.py`, Active UI tests | Full Runner behavioral/system certification | Preserve selection/details/use/read-only lifecycle and prevent unintended reactivation. |
| Telegram topic choice accidentally starts Production | `scripts/test_telegram_production_workflow.py`, `scripts/test_telegram_production_queue.py`, control UI regressions | Full Runner safety/contract certification | Topic selection/approval alone must never dispatch Production. |
| Telegram Production confirmation lacks exact target / queue identity / idempotency | Telegram production workflow/queue/publish-gate tests | Full Runner safety/integration certification | Require explicit exact target and preserve queue/idempotency fail-closed behavior. |
| Telegram webhook auth or duplicate/out-of-order update handling regresses | `scripts/test_telegram_webhook_replay.py`, Edge worker/security tests, outbox tests | Full Runner security/integration certification | Reject unauthorized/replayed-invalid state transitions and preserve ordering/idempotency semantics. |
| Telegram release approval seam regresses | `scripts/test_telegram_release_approval.py`, `scripts/test_telegram_release_identity.py`, release transaction tests | Full Runner + independent release safety seam | Remain fail-closed and separate from general stateful callback routing. |
| Telegram Status/Stats/Final notifications/Rich UI regression | canonical status bridge, YouTube stats, progress/final notify, rich integration/UI test families | Full Runner behavioral/system certification | Preserve rendering/projection/notification contracts without duplicating Full Runner in every specialized workflow. |
| Real Telegram app/device/network/Edge smoke fails after deployment | External/manual or dedicated non-production live smoke | External live evidence | **Not covered by exact-SHA receipt.** Exercise real Telegram buttons/callback delivery without ever sending the literal Production confirmation. A CI Green result must not be reported as 100% device-level proof. |

## Telegram ownership conclusion

At the current architecture boundary, Telegram tests under `scripts/test_*.py` belong to the canonical Full Runner certification owner. This includes the system-level callback/webhook/router regressions as well as queue, idempotency, release, status, notification and Rich UI contracts. Specialized Telegram workflows may retain distinct deployment/live/security evidence, but they must not become another generic Full Runner owner.

The post-deployment phone/app smoke remains deliberately outside the immutable certification receipt because Telegram delivery, network/Edge state and the physical client are mutable external facts.

## Fault-injection closure rule

A rationalization change is acceptable only when every failure class that the previous architecture detected still has an explicit owner after the change. If a protected fault loses its owner, the rationalization fails even when all remaining tests are Green.

No test may be retired merely because another test has a similar name or because total test count is high. Retirement requires a named protected invariant and a stronger current owner.

## Final certification required

After this matrix is committed, the exact final SHA must pass all five integrated evidence owners before merge consideration:

1. Canonical Private Engine / Full Engine + Full Runner.
2. Production Stage Ladder P0-P6.
3. Human Editorial Intent M7.
4. M11 Public-Domain/real renderer integration.
5. Voice Identity Observer V1.

Production is not part of this certification and must not be dispatched by this review.
