from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "telegram_status_contract.json"
TARGET = ROOT / "cloudflare" / "telegram-control-worker" / "status-contract.generated.js"


def render() -> str:
    value = json.loads(SOURCE.read_text(encoding="utf-8"))
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return (
        "// GENERATED from scripts/telegram_status_contract.json. Do not edit by hand.\n"
        f"export const STATUS_CONTRACT = Object.freeze({payload});\n"
    )


def main() -> int:
    TARGET.write_text(render(), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
