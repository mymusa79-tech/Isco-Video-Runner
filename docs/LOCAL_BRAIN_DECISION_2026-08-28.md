# Local Brain — Decision

Date: 2026-08-28
Decision: B — keep isolated as a strategic-independence and disaster-recovery benchmark.

## Canonical status

Local Brain is **not a live production fallback**. It is an isolated benchmark that tests whether a pinned local Arabic-capable model can run within GitHub Actions resource and time constraints without external inference providers.

The canonical benchmark is:

- `.github/workflows/local-brain-benchmark.yml`
- model: Qwen3-4B Q4_K_M GGUF, hash-pinned
- runtime: pinned llama.cpp release
- manual `workflow_dispatch` only

The former `local-brain-benchmark-v2.yml` and `local-brain-benchmark-v3.yml` workflows were superseded implementation iterations of the same benchmark strategy and are removed to eliminate ambiguity and maintenance duplication.

`local-brain-smoke.yml` remains only as a lightweight generic runner smoke test. It is not Local Brain model-quality evidence and is not the canonical benchmark.

## Isolation rule

No production entrypoint may automatically route planning, writing, Gold evaluation, or release decisions to Local Brain. A provider outage must not silently substitute a smaller local model and lower editorial quality merely to make a run technically succeed.

## Runtime correctness

The canonical workflow must preserve the complete llama.cpp runtime bundle beside `llama-server`, including required shared libraries, and must verify the pinned model hash before inference.

## Gate before any future fallback integration

A future proposal to connect Local Brain to production requires a separate review and evidence from a production-representative Arabic corpus. Passing runtime, RAM, disk and latency gates is necessary but insufficient. The comparison must also demonstrate acceptable:

1. Arabic language quality and naturalness.
2. Planner/schema compliance.
3. Topic fidelity and factual/editorial quality.
4. Structural completeness under current production contracts.
5. Failure behavior that is bounded and observable.
6. Quality relative to the current cloud provider path.

Until those quality gates are met, Local Brain remains isolated and manual-only.
