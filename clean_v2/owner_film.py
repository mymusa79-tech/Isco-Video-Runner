"""Owner-written Film ("فيلم جاهز"): his five-part text and his cinematic images.

Same contract as the owner Podcast (text from outside, zero planning/script/tone/factuality
model calls, existing resume door, his images never reviewed or replaced), but the Film keeps
its own character so a viewer feels the difference from the first scene: narrated cinematic
text (no A/B dialogue), a new image every few seconds with varied camera moves instead of
one slow move per beat.

Telegram format:

    فيلم جاهز
    عنوان: ...
    دعوة: جملة واحدة (اختياري)
    نص 1: ...   (the first sentence is the hook)
    نص 2: ...
    نص 3: ...
    نص 4: ...
    نص 5: ...
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from .owner_pack import LONG_TYPES, OwnerPackError, header_marks, normalize_type

PREFIX = "فيلم جاهز"
FORMAT = "film"
SECTION_COUNT = 5
BEATS_PER_SECTION = 3  # the visual-story contract caps a section at 3; cuts happen inside a beat
MIN_WORDS = 300
MAX_TITLE_CHARS = 120
WORDS_PER_MINUTE = 120.0  # display estimate only
DEFAULT_CTA = "شاركنا رأيك في التعليقات."

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_LABEL_RE = re.compile(r"(?:(?<=\s)|^)(?P<label>عنوان|نوع|دعوة|(?:ال)?نص\s*[1-5١-٥])\s*[:：]")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!؟!?])\s+")


def has_prefix(text: str) -> bool:
    return str(text or "").lstrip().startswith(PREFIX)


def _key(label: str) -> str:
    compact = re.sub(r"\s+", "", label).translate(_DIGITS)
    if compact.startswith("ال"):
        compact = compact[2:]
    return {"عنوان": "title", "دعوة": "cta", "نوع": "type"}.get(compact) or "text" + compact[2:]


def parse_film_message(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not has_prefix(raw):
        raise OwnerPackError(f"ابدأ الرسالة بعبارة «{PREFIX}».")
    body = raw[raw.index(PREFIX) + len(PREFIX):]
    marks = header_marks(list(_LABEL_RE.finditer(body)), _key)
    value: dict[str, str] = {}
    for position, match in enumerate(marks):
        key = _key(match.group("label"))
        if key in value:
            raise OwnerPackError(f"الحقل مكرر: {match.group('label').strip()}.")
        end = marks[position + 1].start() if position + 1 < len(marks) else len(body)
        value[key] = " ".join(body[match.end():end].split())
    missing = [
        label
        for key, label in [("title", "عنوان")] + [(f"text{i}", f"نص {i}") for i in range(1, SECTION_COUNT + 1)]
        if not value.get(key)
    ]
    if missing:
        raise OwnerPackError("ينقص في الرسالة: " + "، ".join(missing) + ".")
    title = value["title"]
    if len(title) > MAX_TITLE_CHARS:
        raise OwnerPackError(f"العنوان أطول من {MAX_TITLE_CHARS} حرفًا.")
    texts = [value[f"text{i}"] for i in range(1, SECTION_COUNT + 1)]
    total = sum(len(item.split()) for item in texts)
    if total < MIN_WORDS:
        raise OwnerPackError(f"النص قصير جدًا ({total} كلمة).")
    if re.search(r"(?<!\S)[AB]\s*:", " ".join(texts)):
        raise OwnerPackError("الفيلم سرد بلا أدوار؛ احذف A: و B: (الحوار للبودكاست).")
    cta = value.get("cta") or DEFAULT_CTA
    from clean_v2.contextual_cta import CtaMode, MAX_SPOKEN_WORDS, infer_cta_mode

    mode, _reason = infer_cta_mode(cta)
    if mode == CtaMode.NONE or len(cta.split()) > MAX_SPOKEN_WORDS:
        raise OwnerPackError(f"الدعوة يجب أن تكون جملة واحدة قصيرة بطلب واحد (مثل: {DEFAULT_CTA}).")
    pack: dict[str, Any] = {"schema_version": 1, "format": FORMAT, "title": title, "cta": cta, "texts": texts}
    if value.get("type"):
        pack["type"] = normalize_type(value["type"], LONG_TYPES, "direct_cinematic")
    return pack


def estimated_minutes(pack: Mapping[str, Any]) -> float:
    return sum(len(str(item).split()) for item in pack["texts"]) / WORDS_PER_MINUTE


def _brand_signature() -> tuple[str, str]:
    """The channel's real opener/closer from the Engine policy when it is importable."""
    try:
        from isco_video_agent.config import load_editorial_policy

        signature = load_editorial_policy().get("brand_signature") or {}
        return str(signature.get("opener") or "").strip(), str(signature.get("closer") or "").strip()
    except Exception:
        return "", ""


def _sentences(text: str) -> list[str]:
    return [item.strip() for item in _SENTENCE_SPLIT_RE.split(text) if item.strip()]


def _story(plan: Mapping[str, Any], texts: list[str]) -> dict[str, Any]:
    from clean_v2.visual_story import (
        _default_editorial_signals,
        _default_environment_family,
        _default_shot_role,
        fallback_visual_story,
    )

    story = fallback_visual_story(plan)
    beats: list[dict[str, Any]] = []
    total = SECTION_COUNT * BEATS_PER_SECTION
    for section_index, section in enumerate(plan["sections"]):
        sentences = _sentences(texts[section_index]) or [texts[section_index]]
        for within in range(BEATS_PER_SECTION):
            position = section_index * BEATS_PER_SECTION + within
            role = "hook" if position == 0 else ("payoff" if position == total - 1 else "body")
            query = f"owner cinematic image part {section_index + 1} view {within + 1}"
            anchor = sentences[min(len(sentences) - 1, within * len(sentences) // BEATS_PER_SECTION)]
            beats.append({
                "id": f"b{position + 1}",
                "section_id": str(section["id"]),
                "viewer_intent": f"{anchor[:260]}؛ المشهد {position + 1}",
                "meaning_target": f"{anchor[:260]}؛ المشهد {position + 1}",
                "semantic_must_have": [query],
                "semantic_should_avoid": ["generic mood-only productivity imagery"],
                "shot_intent": query,
                "role": role,
                "stock_query_en": query,
                "display_text_ar": "",
                "source_preference": "stock_motion",
                "shot_role": _default_shot_role(role, within),
                "environment_family": _default_environment_family(query),
                "writer_anchor_ar": anchor,
                **_default_editorial_signals(role),
            })
    story["beats"] = beats
    return story


def build_artifacts(pack: Mapping[str, Any], brief: Mapping[str, Any], out_dir: Path) -> dict[str, Any]:
    from clean_v2.contextual_cta import bind_contextual_cta_to_script
    from clean_v2.contracts import atomic_write_json, validate_plan, validate_script
    from clean_v2.identity_sequence import PRAYER_SENTENCE, assert_spoken_identity, inject_spoken_identity
    from clean_v2.visual_story import validate_visual_story

    out_dir.mkdir(parents=True, exist_ok=True)
    title = str(pack["title"])
    texts = [str(item) for item in pack["texts"]]
    plan = validate_plan(
        {
            "title": title,
            "promise": title,
            "cta": str(pack["cta"]),
            "narrative_format": normalize_type(pack.get("type"), LONG_TYPES, "direct_cinematic"),
            "sections": [
                {
                    "id": f"s{i}",
                    "heading": f"الفصل {i}",
                    "purpose": (_sentences(texts[i - 1]) or [texts[i - 1]])[0][:600],
                    "visual_query_en": f"owner cinematic image part {i}",
                }
                for i in range(1, SECTION_COUNT + 1)
            ],
        },
        {**brief, "format": FORMAT},
    )
    plan["owner_authored"] = True
    plan["_visual_identity_contract"] = "navy_gold_v1"
    atomic_write_json(out_dir / "plan.json", plan)

    script = validate_script(
        {"title": title, "sections": [{"id": f"s{i}", "narration": texts[i - 1]} for i in range(1, SECTION_COUNT + 1)]},
        plan,
    )
    opener, closer = _brand_signature()
    inject_spoken_identity(script["sections"], fmt=FORMAT, opener=opener, closer=closer)
    assert_spoken_identity(script["sections"], fmt=FORMAT, opener=opener, closer=closer)
    atomic_write_json(
        out_dir / "narrative-identity.json",
        {
            "schema_version": 1,
            "source": "clean-v2-film-owner-identity",
            "status": "pass",
            "canonical_opener": opener,
            "canonical_closer": closer,
            "opener": opener,
            "closer": closer,
            "prayer_sentence": PRAYER_SENTENCE,
            "transitions": [],
            "provider_calls_added": 0,
        },
    )
    atomic_write_json(out_dir / "visual-story.json", validate_visual_story(_story(plan, texts), plan, from_checkpoint=True))
    atomic_write_json(out_dir / "brief.json", dict(brief))
    bind_contextual_cta_to_script(output_dir=out_dir, brief=brief, plan=plan, script=script)
    atomic_write_json(out_dir / "script.json", script)
    (out_dir / "narration.txt").write_text(
        "\n\n".join(item["narration"] for item in script["sections"]) + "\n", encoding="utf-8"
    )
    return script
