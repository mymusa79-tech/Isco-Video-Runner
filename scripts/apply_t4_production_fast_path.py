from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / ".github" / "workflows" / "produce-resilient-v4.yml"
BOOTSTRAP_WORKFLOW = ROOT / ".github" / "workflows" / "t4-apply-production-fast-path.yml"
SELF = Path(__file__).resolve()


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"T4 patch anchor {label!r} expected once, found {count}")
    return text.replace(old, new, 1)


def apply() -> None:
    text = PRODUCTION.read_text(encoding="utf-8")

    checkout_anchor = '''      - name: Verify exact Runner checkout
        run: test "$(git rev-parse HEAD)" = "$GITHUB_SHA"
'''
    certification_step = '''      - name: Verify exact Runner checkout
        run: test "$(git rev-parse HEAD)" = "$GITHUB_SHA"

      - name: Require protected exact-SHA production certification
        id: certification_gate
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          set -euo pipefail
          python scripts/production_certification_gate.py \\
            --output "$RUNNER_TEMP/production-certification-gate.json"
'''
    text = _replace_once(text, checkout_anchor, certification_step, "certification gate insertion")

    engine_suite_tail = '''
          engine_test_history="$RUNNER_TEMP/isco-test-state/production-engine-suite/history.json"
          mkdir -p "$(dirname "$engine_test_history")"
          ISCO_HISTORY_PATH="$engine_test_history" python -m unittest discover -s tests -q
'''
    text = _replace_once(text, engine_suite_tail, "", "production Full Engine removal")

    start_marker = "\n      - name: Certify Engine source after production Engine suite\n"
    end_marker = "\n      - name: Isolated dependency vulnerability audit\n"
    start = text.find(start_marker)
    end = text.find(end_marker, start + 1) if start >= 0 else -1
    if start < 0 or end < 0 or end <= start:
        raise SystemExit("T4 full-regression block boundaries were not found exactly")
    text = text[:start] + text[end:]

    diagnostic_anchor = '''            ${{ runner.temp }}/provider-preflight.json
            ${{ runner.temp }}/preproduction-environment.json
'''
    diagnostic_replacement = '''            ${{ runner.temp }}/provider-preflight.json
            ${{ runner.temp }}/preproduction-environment.json
            ${{ runner.temp }}/production-certification-gate.json
'''
    text = _replace_once(text, diagnostic_anchor, diagnostic_replacement, "certification diagnostics")

    forbidden = (
        "python -m unittest discover -s tests -q",
        "find scripts -maxdepth 1 -type f -name 'test_*.py'",
        "Certify Engine source after production Engine suite",
        "Full Runner pre-production regression",
        "Certify Engine source after production Runner suite",
    )
    for needle in forbidden:
        if needle in text:
            raise SystemExit(f"T4 forbidden duplicate regression remains in Production: {needle}")

    required = (
        "Require protected exact-SHA production certification",
        "python scripts/production_certification_gate.py",
        "pip-audit==2.10.1",
        "Restore encrypted cross-run memory",
        "Verify local voice fallback before cloud production",
        "Verify production environment and release namespace",
        "Verify complete provider readiness",
        "Certify provider-portable planning envelope",
        "Produce with task-level brain and voice meshes",
        "Final review and extract result",
    )
    for needle in required:
        if needle not in text:
            raise SystemExit(f"T4 required Production protection missing after patch: {needle}")

    if text.index("Require protected exact-SHA production certification") > text.index("Checkout private engine"):
        raise SystemExit("T4 certification gate must run before private Engine checkout")

    PRODUCTION.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-remove", action="store_true")
    args = parser.parse_args()
    apply()
    if args.self_remove:
        if BOOTSTRAP_WORKFLOW.exists():
            BOOTSTRAP_WORKFLOW.unlink()
        SELF.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
