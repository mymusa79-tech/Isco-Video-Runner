from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
EDITABLE_FIELDS = (
    "topic", "angle", "promise", "hook", "tone", "length", "visual_direction",
    "short_long_strategy", "publish_target",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: object, field: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise ValueError(f"{field} must be non-empty")
    return value


def _canonical_payload(packet: "EditorialDecisionPacket") -> bytes:
    payload = {field: getattr(packet, field) for field in EDITABLE_FIELDS}
    payload.update({"packet_id": packet.packet_id, "version": packet.version, "schema_version": packet.schema_version})
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def packet_sha256(packet: "EditorialDecisionPacket") -> str:
    return hashlib.sha256(_canonical_payload(packet)).hexdigest()


@dataclass(frozen=True)
class EditorialDecisionPacket:
    packet_id: str
    version: int
    topic: str
    angle: str
    promise: str
    hook: str
    tone: str
    length: str
    visual_direction: str
    short_long_strategy: str
    publish_target: str
    change_summary: str
    created_at: str
    approved_by_user: bool = False
    approved_at: str = ""
    schema_version: int = SCHEMA_VERSION

    @property
    def version_label(self) -> str:
        return f"V{self.version}"

    @property
    def sha256(self) -> str:
        return packet_sha256(self)


@dataclass(frozen=True)
class AlternativeSet:
    packet_id: str
    base_version: int
    field: str
    values: tuple[str, ...]


class EditorialPacketStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root) / "editorial_packets"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, packet_id: str) -> Path:
        safe = _text(packet_id, "packet_id")
        if any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in safe):
            raise ValueError("packet_id contains unsupported characters")
        return self.root / f"{safe}.json"

    def _atomic(self, path: Path, payload: Any) -> None:
        fd, temp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def _read(self, packet_id: str) -> list[EditorialDecisionPacket]:
        path = self._path(packet_id)
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise RuntimeError("editorial packet history is malformed")
        return [EditorialDecisionPacket(**item) for item in raw]

    def _write(self, packet_id: str, history: list[EditorialDecisionPacket]) -> None:
        self._atomic(self._path(packet_id), [asdict(item) for item in history])

    def create(self, *, packet_id: str, **fields: str) -> EditorialDecisionPacket:
        packet_id = _text(packet_id, "packet_id")
        if self._read(packet_id):
            raise ValueError("packet_id already exists")
        missing = [field for field in EDITABLE_FIELDS if not str(fields.get(field) or "").strip()]
        if missing:
            raise ValueError("missing packet fields: " + ", ".join(missing))
        packet = EditorialDecisionPacket(
            packet_id=packet_id,
            version=1,
            change_summary="Initial editorial decision packet",
            created_at=_now(),
            **{field: _text(fields[field], field) for field in EDITABLE_FIELDS},
        )
        self._write(packet_id, [packet])
        return packet

    def history(self, packet_id: str) -> tuple[EditorialDecisionPacket, ...]:
        return tuple(self._read(packet_id))

    def current(self, packet_id: str) -> EditorialDecisionPacket:
        history = self._read(packet_id)
        if not history:
            raise KeyError(packet_id)
        return history[-1]

    def edit(self, packet_id: str, *, expected_version: int, changes: dict[str, str]) -> EditorialDecisionPacket:
        current = self.current(packet_id)
        if current.version != int(expected_version):
            raise RuntimeError("editorial packet version conflict")
        if not changes:
            raise ValueError("at least one packet field must change")
        unknown = set(changes) - set(EDITABLE_FIELDS)
        if unknown:
            raise ValueError("unsupported packet field(s): " + ", ".join(sorted(unknown)))
        normalized = {field: _text(value, field) for field, value in changes.items()}
        effective = {field: value for field, value in normalized.items() if getattr(current, field) != value}
        if not effective:
            raise ValueError("packet edit does not change any value")
        summary = "; ".join(f"{field}: {getattr(current, field)} → {value}" for field, value in effective.items())
        updated = replace(
            current,
            version=current.version + 1,
            change_summary=summary,
            created_at=_now(),
            approved_by_user=False,
            approved_at="",
            **effective,
        )
        history = list(self.history(packet_id))
        history.append(updated)
        self._write(packet_id, history)
        return updated

    def alternatives(self, packet_id: str, *, expected_version: int, field: str, values: Iterable[str]) -> AlternativeSet:
        current = self.current(packet_id)
        if current.version != int(expected_version):
            raise RuntimeError("editorial packet version conflict")
        if field not in EDITABLE_FIELDS:
            raise ValueError("unsupported alternative field")
        normalized = tuple(_text(value, field) for value in values)
        if len(normalized) != 3 or len(set(normalized)) != 3:
            raise ValueError("exactly three distinct alternatives are required")
        return AlternativeSet(packet_id, current.version, field, normalized)

    def choose_alternative(self, alternatives: AlternativeSet, index: int) -> EditorialDecisionPacket:
        if not 0 <= int(index) < len(alternatives.values):
            raise IndexError(index)
        return self.edit(
            alternatives.packet_id,
            expected_version=alternatives.base_version,
            changes={alternatives.field: alternatives.values[int(index)]},
        )

    def approve(self, packet_id: str, *, expected_version: int) -> EditorialDecisionPacket:
        current = self.current(packet_id)
        if current.version != int(expected_version):
            raise RuntimeError("editorial packet version conflict")
        approved = replace(current, approved_by_user=True, approved_at=_now())
        history = list(self.history(packet_id))
        history[-1] = approved
        self._write(packet_id, history)
        return approved


def bind_to_production_request(request: dict[str, Any], packet: EditorialDecisionPacket) -> dict[str, Any]:
    """Return a copy carrying an immutable exact-version editorial binding.

    This is intentionally additive. It does not start production and never changes
    the existing publish approval contract.
    """
    if packet.approved_by_user is not True:
        raise ValueError("production binding requires an explicitly approved packet version")
    bound = json.loads(json.dumps(request, ensure_ascii=False))
    if not isinstance(bound, dict):
        raise TypeError("production request must be an object")
    bound["editorial_packet_binding"] = {
        "packet_id": packet.packet_id,
        "version": packet.version,
        "sha256": packet.sha256,
        "approved_at": packet.approved_at,
    }
    return bound


def verify_production_binding(request: dict[str, Any], packet: EditorialDecisionPacket) -> bool:
    binding = request.get("editorial_packet_binding") if isinstance(request, dict) else None
    return bool(
        isinstance(binding, dict)
        and binding.get("packet_id") == packet.packet_id
        and binding.get("version") == packet.version
        and binding.get("sha256") == packet.sha256
        and packet.approved_by_user is True
    )


def render_telegram(packet: EditorialDecisionPacket) -> tuple[str, list[list[dict[str, str]]]]:
    lines = [f"📝 Editorial Decision Packet · {packet.version_label}", ""]
    labels = {
        "topic":"Topic", "angle":"Angle", "promise":"Promise", "hook":"Hook", "tone":"Tone",
        "length":"Length", "visual_direction":"Visual", "short_long_strategy":"Strategy", "publish_target":"Publish target",
    }
    for field in EDITABLE_FIELDS:
        lines.append(f"{labels[field]}: {getattr(packet, field)}")
    if packet.version > 1:
        lines.extend(["", f"Δ {packet.change_summary}"])
    lines.extend(["", "✅ Approved" if packet.approved_by_user else "⏳ Awaiting editorial decision"])
    prefix = f"edp:{packet.packet_id}:{packet.version}"
    keyboard = [
        [{"text":"✏️ Change Hook", "callback_data":f"{prefix}:hook"}, {"text":"↪️ Keep topic, new angle", "callback_data":f"{prefix}:angle"}],
        [{"text":"3️⃣ Give me 3 alternatives", "callback_data":f"{prefix}:alts"}],
    ]
    if not packet.approved_by_user:
        keyboard.append([{"text":"✅ Approve & Produce", "callback_data":f"{prefix}:approve"}])
    for row in keyboard:
        for button in row:
            if len(button["callback_data"].encode("utf-8")) > 64:
                raise ValueError("Editorial callback exceeds Telegram 64-byte limit")
    return "\n".join(lines), keyboard
