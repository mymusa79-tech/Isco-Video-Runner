# Pre-Production Certification V2

This certification is intentionally non-production. It may run tests, dependency resolution, read-only provider authentication/model checks when the real Production workflow is later dispatched, and static/dynamic environment contracts. It must never dispatch `produce-resilient-v4.yml` itself.

The permanent controls cover provider credential/model drift, dependency/environment drift after Piper installation, GitHub Release rerun collisions, system media-tool availability, workflow trigger/checkout invariants, existing Reliability Kernel retry ownership, checkpoint isolation, runtime script/import identity, secret cleanup, and manual YouTube publication.

External comparison uses public documentation and public issue trackers only. It does not rely on or claim access to other customers' private incidents.

The Gemini September 2026 authentication-key transition is tracked as an account-side deadline risk. Live provider preflight can prove the current credential is accepted and required models are visible; it cannot infer a key type that the provider does not expose through the models endpoint.
