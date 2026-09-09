# Run #230 QC_PENDING / Telegram closure

Run #230 proved that Final Master can pass and the exact Gold QC_PENDING checkpoint can be captured while terminal Telegram reconciliation still classifies the consumed production dispatch as a generic failure.

This closure keeps the existing fail-closed Gold authority and completes the Run #225 recovery design without rerendering:

- `opening-visual-audit.json` is post-Gold evidence and is optional in a recovery bundle when Vision capacity fails while that audit is being created.
- QC_PENDING capture builds and validates an exact resume bundle before the production process unwinds.
- The bundle is placed below the existing `short-*` diagnostics allowlist, so canonical V4's already-existing failure artifact carries it without a second upload attempt.
- terminal V4 reconciliation promotes only an exact current-run, current-Runner, SHA-bound bundle to `qc_pending`; all other failures keep the generic fail-closed path.
- the production ledger stores a logical `isco-qc-pending-diagnostics-<run_number>` locator; the one-time Gold authorization resolves only that explicit locator to `isco-resilient-v4-diagnostics-<run_number>`.
- Telegram terminal UX shows `Final Master محفوظ` and a direct `▶️ تابع Gold` callback for QC_PENDING only.
- no planning, research, retrieval, TTS, render, quality threshold, release authority, or YouTube publication policy changes.

Run #230 itself cannot be reconstructed into the new exact recovery bundle from its historical diagnostics because the old workflow did not upload `qc-pending.json` / its bound production-history record. The closure is preventive for the next exact occurrence; any recovery of #230 must not invent that missing evidence.
