from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Mapping


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SUPPORTED_FORMATS = frozenset({"film", "moment", "story", "short", "podcast"})
_HASH_METADATA_KEYS = frozenset({"approved_hash", "brief_sha256"})


class ContractError(RuntimeError):
    pass


def _canonical_json_value(value: Any, *, path: str = "$") -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return unicodedata.normalize("NFC", value) if isinstance(value, str) else value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError(f"brief contains non-finite number at {path}")
        return value
    if isinstance(value, list):
        return [
            _canonical_json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        canonical: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError(f"brief contains non-string key at {path}")
            normalized = unicodedata.normalize("NFC", key)
            if normalized in canonical:
                raise ContractError(f"brief contains duplicate canonical key at {path}")
            canonical[normalized] = _canonical_json_value(
                item, path=f"{path}.{normalized}"
            )
        return canonical
    raise ContractError(f"brief contains unsupported type at {path}: {type(value).__name__}")


def canonicalize_brief(brief: Mapping[str, Any]) -> bytes:
    if not isinstance(brief, Mapping):
        raise ContractError("brief must be a JSON object")
    subject = {
        key: value for key, value in brief.items() if key not in _HASH_METADATA_KEYS
    }
    canonical = _canonical_json_value(subject)
    return json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def compute_brief_sha256(brief: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonicalize_brief(brief)).hexdigest()


def load_approved_brief(path: Path, approved_sha256: str) -> dict[str, Any]:
    if not SHA256_RE.fullmatch(str(approved_sha256 or "")):
        raise ContractError("approved brief SHA-256 must be exactly 64 lowercase hex characters")
    try:
        brief = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read approved brief: {type(exc).__name__}") from exc
    if not isinstance(brief, dict):
        raise ContractError("brief must be a JSON object")
    if brief.get("approved_by_user") is not True:
        raise ContractError("brief requires explicit user approval")
    actual = compute_brief_sha256(brief)
    if not hmac.compare_digest(actual, approved_sha256):
        raise ContractError("brief changed after approval")
    topic = str(brief.get("approved_topic") or "").strip()
    fmt = str(brief.get("format") or "").strip().lower()
    if not topic:
        raise ContractError("approved_topic is required")
    if fmt not in SUPPORTED_FORMATS:
        raise ContractError(f"unsupported approved format: {fmt or '<empty>'}")
    brief["format"] = fmt
    return brief


def require_exact_engine_sha(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not SHA_RE.fullmatch(normalized):
        raise ContractError("Engine pin must be one exact 40-character commit SHA")
    return normalized


def validate_plan(value: Any, brief: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("planning output must be a JSON object")
    title = str(value.get("title") or "").strip()
    promise = str(value.get("promise") or "").strip()
    cta = str(value.get("cta") or "").strip()
    raw_sections = value.get("sections")
    if not title or not promise or not isinstance(raw_sections, list):
        raise ContractError("plan requires title, promise, and sections")
    fmt = str(brief.get("format") or "")
    if fmt == "short" and cta:
        raise ContractError("short plan requires an empty social cta")
    if fmt not in {"moment", "short"}:
        if not cta:
            raise ContractError("plan requires one non-empty contextual cta for CTA-enabled formats")
        from .contextual_cta import CtaMode, infer_cta_mode

        cta_mode, cta_reason = infer_cta_mode(cta)
        if cta_mode == CtaMode.NONE:
            raise ContractError(
                f"plan contextual cta must contain exactly one supported action: {cta_reason}"
            )
    if fmt == "film":
        if len(raw_sections) != 5:
            raise ContractError("plan section count must be exactly 5 for film")
    elif fmt == "podcast":
        if not 2 <= len(raw_sections) <= 5:
            raise ContractError("plan section count must be between 2 and 5 for podcast")
    elif fmt == "short":
        if len(raw_sections) != 3:
            raise ContractError("plan section count must be exactly 3 for short")
    elif not 1 <= len(raw_sections) <= 5:
        raise ContractError(
            f"plan section count must be between 1 and 5 for {fmt}"
        )
    sections: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_sections, start=1):
        if not isinstance(raw, dict):
            raise ContractError(f"plan section {index} must be an object")
        section_id = str(raw.get("id") or f"s{index}").strip()[:40]
        heading = str(raw.get("heading") or "").strip()
        purpose = str(raw.get("purpose") or "").strip()
        query = str(raw.get("visual_query_en") or "").strip()
        alt_query = str(raw.get("visual_query_alt_en") or "").strip()
        if len(query) > 260:
            raise ContractError(
                f"plan section {section_id} visual_query_en exceeds 260 characters"
            )
        if fmt == "short":
            # Fresh provider outputs are required by the strict response schema to
            # supply this field. The deterministic fallback below exists only so
            # older saved/resumed plans do not become unreadable after the upgrade.
            if not alt_query:
                alt_query = f"{query} close detail"[:260]
            if len(alt_query) > 260:
                raise ContractError(
                    f"plan section {section_id} visual_query_alt_en exceeds 260 characters"
                )
            if " ".join(query.casefold().split()) == " ".join(alt_query.casefold().split()):
                raise ContractError(
                    f"plan section {section_id} visual queries must be distinct"
                )
        if purpose.count("(") != purpose.count(")") or purpose.count("«") != purpose.count("»"):
            raise ContractError(f"plan section {section_id} purpose looks truncated")
        if not section_id or section_id in seen:
            raise ContractError("plan section ids must be unique and non-empty")
        if not heading or not purpose or not query:
            raise ContractError(f"plan section {section_id} is incomplete")
        seen.add(section_id)
        section_value = {
            "id": section_id,
            "heading": heading[:240],
            "purpose": purpose[:800],
            "visual_query_en": query,
        }
        if fmt == "short":
            section_value["visual_query_alt_en"] = alt_query
        sections.append(section_value)
    return {
        "schema_version": 1,
        "title": title[:300],
        "promise": promise[:800],
        "cta": cta[:700],
        "sections": sections,
    }


def validate_script(value: Any, plan: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("script output must be a JSON object")
    raw_sections = value.get("sections")
    if not isinstance(raw_sections, list):
        raise ContractError("script requires a sections array")
    expected = [str(item["id"]) for item in plan["sections"]]
    normalized: list[dict[str, str]] = []
    for raw in raw_sections:
        if not isinstance(raw, dict):
            raise ContractError("every script section must be an object")
        section_id = str(raw.get("id") or "").strip()
        narration = str(raw.get("narration") or "").strip()
        if not section_id or len(narration) < 20:
            raise ContractError("every script section needs an id and non-empty narration")
        normalized.append({"id": section_id, "narration": narration})
    actual = [item["id"] for item in normalized]
    if actual != expected:
        raise ContractError(
            f"script section ids/order must match plan exactly: expected={expected} actual={actual}"
        )
    return {
        "schema_version": 1,
        "title": str(value.get("title") or plan.get("title") or "").strip()[:300],
        "sections": normalized,
    }


def validate_narrative_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("narrative identity output must be a JSON object")
    opener = str(value.get("opener") or "").strip()
    closer = str(value.get("closer") or "").strip()
    raw_transitions = value.get("transitions")
    if not opener or not closer or not isinstance(raw_transitions, list):
        raise ContractError("narrative identity requires opener, closer, and transitions")
    if len(raw_transitions) != 3:
        raise ContractError("narrative identity requires exactly 3 transitions")
    transitions = [str(item or "").strip() for item in raw_transitions]
    if any(not item for item in transitions):
        raise ContractError("narrative identity transitions must be non-empty")
    return {
        "schema_version": 1,
        "opener": opener[:600],
        "closer": closer[:600],
        "transitions": [item[:200] for item in transitions],
    }


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
