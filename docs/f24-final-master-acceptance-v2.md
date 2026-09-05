# F24 — Final Master Acceptance Contract V2

`final.master.acceptance.v2` is the enforcing exact-artifact contract at the P4→P5 boundary.

## Authority

- The certified `scripts/final_master_qc.py` core remains byte-identical under its existing L7 contract.
- The stable `scripts/orchestration_qc_port.py` remains byte-identical and keeps its existing Stage Registry implementation binding.
- F24 is installed as a mandatory runtime wrapper above that stable port; durable Final QC reuse is optional and never becomes F24 authority.
- `final-master-qc.json` remains the single P4 receipt; no parallel sidecar certificate exists.
- After a fresh certified-core PASS, or after restoration of an exact durable core PASS, F24 re-probes current `final.mp4`, applies upload-conformance checks, atomically upgrades the receipt, and immediately revalidates it.
- The V2 receipt binds exact byte identities for `final.mp4`, `plan.json`, `quality-final.json`, and `visual-timeline.json`.
- The receipt also binds the certified Final Master QC implementation SHA and an F24 policy fingerprint.
- Optional Audio Production / Producer repair evidence present at P4 is byte-bound into the receipt.

## Handoff

- Production cannot return P4 success through the runtime wrapper until the V2 receipt is sealed and revalidated.
- Gold revalidates a present P4 receipt and requires its certified `final.mp4` SHA to equal the bytes entering Gold.
- Gold retains its existing same-render invariant and rejects any final-media mutation before acceptance.
- Unified Delivery requires the current primary artifact set to match the P4 receipt.
- Renamed/staged sibling Shorts are verified by certified final-video SHA and byte length before entering the unified delivery bundle.
- Planning and sibling-planning orchestration do not import F24, so Final Master acceptance code cannot widen the durable Planning checkpoint dependency closure.

## Upload conformance

The certified core continues to own its existing H.264/yuv420p/30fps/AAC/48k and perceptual/decode gates. F24 adds only post-core delivery conformance: explicit non-High H.264 profile, contradictory field order, non-LC AAC profile, explicit HDR metadata, or fully reported non-BT.709 SDR tags block. Missing optional profile/color metadata remains visible as warnings. MP4 fast-start is recorded and remains warning-only because it is a delivery optimization rather than media-integrity corruption.

## Stage Ladder

- P4 runs the exact immutable `video-50` delivery-integrity fixture through certified core → F24 composition and proves the certified final SHA equals the fixture SHA without media mutation. Run50 predates the current montage stack, so this replay is not visual-quality evidence for current Short or long output.
- P5 proves Gold accepts the same bytes and preserves its same-render invariant.
- P6 proves Unified Delivery still carries that exact P4-certified SHA while publication remains false.
- F24-specific unit/fault tests run in P4; existing historical family evidence is preserved rather than rewritten.

## Invariants

- Zero new AI calls.
- No new repair owner and no media mutation in P4.
- No quality threshold is lowered.
- Long and standalone Short share the same F24 contract.
- Sibling Short copies preserve the original P4 byte identity after renaming at delivery.
- Publication remains manual; this contract does not authorize a Production Run or YouTube publication.
