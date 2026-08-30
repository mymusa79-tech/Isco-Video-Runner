from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "isco.ci.canonical-full-regression-receipt.v1"
OWNER = "verify-private-engine.yml"
REQUIRED_GREEN_EVIDENCE = (
    "dependency_audit",
    "run126_capacity_snapshot",
    "run104_structural_repair",
    "full_engine",
    "approved_brief_cli",
    "standalone_short_v2",
    "full_runner",
    "exact_closure_delta",
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class RegressionReceiptError(ValueError):
    pass


def _require_sha(value: str, field: str) -> str:
    if not _SHA_RE.fullmatch(value):
        raise RegressionReceiptError(f"{field} must be an exact lowercase 40-character SHA")
    return value


def certification_tag(runner_sha: str, engine_sha: str) -> str:
    runner_sha = _require_sha(runner_sha, "runner_sha")
    engine_sha = _require_sha(engine_sha, "engine_sha")
    return f"canonical-full-regression-green-{runner_sha}-{engine_sha}"


def build_receipt(runner_sha: str, engine_sha: str) -> dict[str, Any]:
    runner_sha = _require_sha(runner_sha, "runner_sha")
    engine_sha = _require_sha(engine_sha, "engine_sha")
    evidence = {name: True for name in REQUIRED_GREEN_EVIDENCE}
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "status": "green",
        "runner_sha": runner_sha,
        "engine_sha": engine_sha,
        "evidence": evidence,
        "production_dispatch_performed": False,
        "certification_tag": certification_tag(runner_sha, engine_sha),
    }


def validate_receipt(
    payload: Mapping[str, Any],
    *,
    runner_sha: str | None = None,
    engine_sha: str | None = None,
) -> dict[str, Any]:
    expected_keys = {
        "schema",
        "owner",
        "status",
        "runner_sha",
        "engine_sha",
        "evidence",
        "production_dispatch_performed",
        "certification_tag",
    }
    if set(payload) != expected_keys:
        missing = sorted(expected_keys - set(payload))
        extra = sorted(set(payload) - expected_keys)
        raise RegressionReceiptError(f"receipt keys mismatch: missing={missing} extra={extra}")

    if payload["schema"] != SCHEMA:
        raise RegressionReceiptError("unsupported receipt schema")
    if payload["owner"] != OWNER:
        raise RegressionReceiptError("unexpected receipt owner")
    if payload["status"] != "green":
        raise RegressionReceiptError("receipt status must be green")
    if payload["production_dispatch_performed"] is not False:
        raise RegressionReceiptError("certification must not dispatch production")

    actual_runner = _require_sha(str(payload["runner_sha"]), "runner_sha")
    actual_engine = _require_sha(str(payload["engine_sha"]), "engine_sha")
    if runner_sha is not None and actual_runner != _require_sha(runner_sha, "expected_runner_sha"):
        raise RegressionReceiptError("receipt Runner SHA mismatch")
    if engine_sha is not None and actual_engine != _require_sha(engine_sha, "expected_engine_sha"):
        raise RegressionReceiptError("receipt Engine SHA mismatch")

    evidence = payload["evidence"]
    if not isinstance(evidence, Mapping):
        raise RegressionReceiptError("receipt evidence must be an object")
    if set(evidence) != set(REQUIRED_GREEN_EVIDENCE):
        missing = sorted(set(REQUIRED_GREEN_EVIDENCE) - set(evidence))
        extra = sorted(set(evidence) - set(REQUIRED_GREEN_EVIDENCE))
        raise RegressionReceiptError(f"receipt evidence mismatch: missing={missing} extra={extra}")
    failed = sorted(name for name in REQUIRED_GREEN_EVIDENCE if evidence.get(name) is not True)
    if failed:
        raise RegressionReceiptError(f"receipt evidence is not fully green: {failed}")

    expected_tag = certification_tag(actual_runner, actual_engine)
    if payload["certification_tag"] != expected_tag:
        raise RegressionReceiptError("receipt certification tag mismatch")

    return dict(payload)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RegressionReceiptError("receipt root must be an object")
    return value


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or validate the exact-SHA canonical regression receipt")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create")
    create.add_argument("--runner-sha", required=True)
    create.add_argument("--engine-sha", required=True)
    create.add_argument("--output", type=Path, required=True)

    validate = sub.add_parser("validate")
    validate.add_argument("--input", type=Path, required=True)
    validate.add_argument("--runner-sha", required=True)
    validate.add_argument("--engine-sha", required=True)

    args = parser.parse_args()
    if args.command == "create":
        payload = build_receipt(args.runner_sha, args.engine_sha)
        validate_receipt(payload, runner_sha=args.runner_sha, engine_sha=args.engine_sha)
        _write_json(args.output, payload)
        return 0

    payload = _read_json(args.input)
    validate_receipt(payload, runner_sha=args.runner_sha, engine_sha=args.engine_sha)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
