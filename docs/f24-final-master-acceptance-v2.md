# F24 — Final Master Acceptance Contract V2

`final.master.acceptance.v2` is the enforcing exact-artifact contract at the P4 boundary.

## Authority

- `final-master-qc.json` is the single P4 receipt; no parallel sidecar certificate exists.
- The receipt is written atomically and binds exact byte identities for `final.mp4`, `plan.json`, `quality-final.json`, and `visual-timeline.json`.
- The receipt also binds the current Final Master QC policy fingerprint and implementation SHA.
- Optional Audio Production / Producer repair evidence present at P4 is byte-bound into the receipt.

## Handoff

- The canonical QC port revalidates the receipt before returning P4 success.
- Gold revalidates a present P4 receipt and requires its certified `final.mp4` SHA to equal the bytes entering Gold.
- Gold retains its existing same-render invariant and rejects any final-media mutation before acceptance.
- Unified Delivery requires the current primary artifact set to match the P4 receipt.
- Renamed/staged sibling Shorts are verified by certified final-video SHA and byte length before entering the unified delivery bundle.

## Upload conformance

P4 records and enforces the existing H.264/yuv420p/30fps/AAC/48k stream rules, adds explicit H.264 High-profile, progressive-field-order, AAC-LC and BT.709 checks when those values are reported, and records MP4 fast-start status. Missing profile/color metadata remains visible as warnings; explicit contradictory metadata blocks. Fast-start remains a warning because it is a delivery optimization rather than media-integrity corruption.

## Invariants

- Zero new AI calls.
- No new repair owner and no media mutation in P4.
- Long and standalone Short share the same contract.
- Sibling Short copies preserve the original P4 byte identity after renaming.
- Publication remains manual; this contract does not authorize a Production Run or YouTube publication.
