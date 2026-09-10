from __future__ import annotations

from pathlib import Path


WORKFLOW = Path('.github/workflows/produce-resilient-v4.yml')
TEMP_WORKFLOW = Path('.github/workflows/_patch-visual-failure-memory-transport.yml')
SELF = Path(__file__)


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f'{label} anchor count != 1: {count}')
    return text.replace(old, new, 1)


def main() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')

    restore_anchor = '''          echo "ISCO_HISTORY_PATH=$state_dir/history.json" >> "$GITHUB_ENV"

      - name: Require healthy restored cross-run memory
'''
    restore_replacement = '''          echo "ISCO_HISTORY_PATH=$state_dir/history.json" >> "$GITHUB_ENV"

      - name: Snapshot restored cross-run memory baseline
        id: snapshot_state_baseline
        if: steps.restore_state.outputs.save_allowed == 'true'
        run: |
          set -euo pipefail
          test -n "${ISCO_HISTORY_PATH:-}"
          test -f "$ISCO_HISTORY_PATH"
          cp "$ISCO_HISTORY_PATH" "$RUNNER_TEMP/isco-state/history.restored-baseline.json"
          chmod 600 "$RUNNER_TEMP/isco-state/history.restored-baseline.json"

      - name: Require healthy restored cross-run memory
'''
    text = replace_once(text, restore_anchor, restore_replacement, label='restore')

    persist_anchor = '''          python scripts/state_persistence_strict.py \\
            --repo state-writer \\
            --encrypted "$encrypted" \\
            --branch agent-state \\
            --run-number "$GITHUB_RUN_NUMBER" \\
            --report "$RUNNER_TEMP/state-persistence.json"

      - name: Upload state closure diagnostics
'''
    persist_replacement = '''          python scripts/state_persistence_strict.py \\
            --repo state-writer \\
            --encrypted "$encrypted" \\
            --branch agent-state \\
            --run-number "$GITHUB_RUN_NUMBER" \\
            --report "$RUNNER_TEMP/state-persistence.json"

      - name: Prepare failed-run visual learning checkpoint
        id: prepare_visual_failure_checkpoint
        if: >-
          always() &&
          steps.restore_state.outputs.save_allowed == 'true' &&
          steps.snapshot_state_baseline.outcome == 'success' &&
          steps.produce_video.outcome != 'skipped' &&
          steps.persist_state.outcome == 'skipped'
        continue-on-error: true
        run: |
          set -euo pipefail
          test -n "${ISCO_HISTORY_PATH:-}"
          python scripts/visual_failure_memory_checkpoint.py \\
            --baseline "$RUNNER_TEMP/isco-state/history.restored-baseline.json" \\
            --runtime "$ISCO_HISTORY_PATH" \\
            --output "$RUNNER_TEMP/isco-state/history.visual-failure-checkpoint.json" \\
            --github-output "$GITHUB_OUTPUT"

      - name: Checkout agent-state writer for failed-run visual learning
        id: checkout_visual_failure_state_writer
        if: >-
          always() &&
          steps.prepare_visual_failure_checkpoint.outcome == 'success' &&
          steps.prepare_visual_failure_checkpoint.outputs.changed == 'true'
        continue-on-error: true
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
        with:
          path: failure-state-writer
          persist-credentials: true
          fetch-depth: 1

      - name: Persist failed-run visual learning only
        id: persist_visual_failure_checkpoint
        if: >-
          always() &&
          steps.prepare_visual_failure_checkpoint.outcome == 'success' &&
          steps.prepare_visual_failure_checkpoint.outputs.changed == 'true' &&
          steps.checkout_visual_failure_state_writer.outcome == 'success'
        continue-on-error: true
        env:
          STATE_ENCRYPTION_KEY: ${{ secrets.STATE_ENCRYPTION_KEY }}
        run: |
          set -euo pipefail
          test -n "$STATE_ENCRYPTION_KEY"
          checkpoint="$RUNNER_TEMP/isco-state/history.visual-failure-checkpoint.json"
          encrypted="$RUNNER_TEMP/isco-state/history.visual-failure-checkpoint.json.enc"
          test -f "$checkpoint"
          python scripts/persistent_memory.py encrypt \\
            --plain "$checkpoint" \\
            --encrypted "$encrypted"
          python scripts/state_persistence_strict.py \\
            --repo failure-state-writer \\
            --encrypted "$encrypted" \\
            --branch agent-state \\
            --run-number "$GITHUB_RUN_NUMBER" \\
            --report "$RUNNER_TEMP/visual-failure-state-persistence.json"

      - name: Upload failed-run visual learning persistence diagnostics
        if: >-
          always() &&
          (steps.prepare_visual_failure_checkpoint.outcome == 'failure' ||
           steps.checkout_visual_failure_state_writer.outcome == 'failure' ||
           steps.persist_visual_failure_checkpoint.outcome == 'failure')
        continue-on-error: true
        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a
        with:
          name: isco-visual-failure-state-${{ github.run_number }}
          path: ${{ runner.temp }}/visual-failure-state-persistence.json
          if-no-files-found: warn
          retention-days: 7

      - name: Upload state closure diagnostics
'''
    text = replace_once(text, persist_anchor, persist_replacement, label='persistence')

    # The normal approved-state path and the failed-run learning path must be mutually
    # exclusive so both can never try to advance the same authenticated lineage.
    required_fragments = (
        "id: snapshot_state_baseline",
        "id: prepare_visual_failure_checkpoint",
        "steps.persist_state.outcome == 'skipped'",
        "--baseline \"$RUNNER_TEMP/isco-state/history.restored-baseline.json\"",
        "--repo failure-state-writer",
    )
    for fragment in required_fragments:
        if text.count(fragment) != 1:
            raise SystemExit(f'patched workflow invariant count != 1 for: {fragment}')

    WORKFLOW.write_text(text, encoding='utf-8')
    TEMP_WORKFLOW.unlink()
    SELF.unlink()


if __name__ == '__main__':
    main()
