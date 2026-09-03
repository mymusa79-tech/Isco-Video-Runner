# Planning Production Contract V2

F23 closes Planning as one Long + Standalone Short production family without replacing the existing Stage Contract, provider router, cache owner, Producer repair owner, or quality gates.

Runtime invariants:
- accepted Stage responses are bound into one run-local lineage receipt set;
- deadline policy is provider-visible contract identity and therefore changes cache fingerprints;
- authentication/configuration failures are classified as `AUTH_CONFIG` and are never retryable;
- Long and Short have explicit family wall-clock acceptance ceilings;
- the exact final `plan.json` plus semantic digest, approved brief topic/format/bytes, research provenance, final planning-gate evidence, Runner planning-runtime closure, Runner SHA and Engine SHA are certified before Director/TTS/Visual work can begin;
- the observe-only Director boundary is revalidated after it returns, so TTS/Short Voice/Visual cannot start if `plan.json` or its certified evidence changed;
- the narrow historical product-proof fallback has its own explicit local lineage; partial cloud Stage receipts are retained only as discarded pre-fallback evidence and are never claimed as the source of the fallback plan;
- a later `plan_source` annotation is the only supported post-certification plan mutation and must preserve the semantic digest while rebinding the file SHA;
- this contract does not dispatch production and does not loosen any existing quality gate.
