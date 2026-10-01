from __future__ import annotations

import copy
import re
from typing import Any, Mapping


CHANNEL_VISUAL_IDENTITY = (
    "Grounded cinematic realism with quiet premium depth; restrained dark navy and charcoal shadows; "
    "ivory-neutral highlights; warm gold only as a rare accent; moderate-to-deep natural exposure; "
    "soft practical directional light; tactile real environments; no blanket blue wash; "
    "a wakeful visual signature built on observable state-change from friction toward clarity, movement "
    "or earned progress, never on one repeated prop; hope shown through effort and earned small wins "
    "rather than glossy lifestyle brightness or forced melancholy; environments, hands, objects, routines, "
    "back views and wide shots; no identifiable faces."
)
VISUAL_WORLD_DEFAULT = CHANNEL_VISUAL_IDENTITY
CHANNEL_VISUAL_AVOID = (
    "bright lifestyle advertising",
    "flat beige or washed-out warm-neutral stock look",
    "cold neon or heavy blue color cast",
    "generic stock-happy imagery",
    "generic productivity desk or writing imagery unless it is the exact semantic action",
    "repetitive stationery, notebooks, sticky notes, or typing across consecutive beats",
    "gloomy or depressive treatment",
)
SOURCE_PREFERENCES = frozenset({"stock_motion", "stock_still", "ai_still"})
BEAT_ROLES = frozenset({"hook", "body", "payoff"})
MAX_BEATS_PER_SECTION = 3
MAX_AI_STILL_BEATS = 4

# Three tiny semantic signals let the existing renderer behave more like a human
# editor without becoming a second creative authority. They never add duration,
# random timing, provider calls, or new scenes.
HOLD_REASONS = frozenset({
    "idea_continues",
    "idea_changes",
    "hook_progression",
    "payoff_landing",
})
PAUSE_INTENTS = frozenset({"none", "micro", "emphasis", "transition", "ending"})
AUDIO_ENERGIES = frozenset({"quiet", "low", "steady", "lift", "resolve"})
SHOT_ROLES = frozenset({"establish", "detail", "action", "consequence", "payoff"})
EDITORIAL_HOLD_WEIGHTS = {
    "idea_continues": 1.25,
    "idea_changes": 1.00,
    "hook_progression": 0.95,
    "payoff_landing": 1.20,
}
_PRAYER_TEXT_MARKERS = ("اللهم", "محمد")

_WRITER_INTENT_DROP_TOKENS = frozenset({
    "cinematic", "warm", "neutral", "lighting", "light", "shot", "frame",
    "composition", "depth", "foreground", "background", "soft", "natural",
    "premium", "dramatic", "emotional", "inspiring", "motivational", "beautiful",
    "text", "texts", "word", "words", "caption", "captions",
    "subtitle", "subtitles", "title", "titles", "quote", "quotes",
    "label", "labels", "lettering", "typography", "watermark", "watermarks",
    "logo", "logos",
})
_EMBEDDED_TEXT_REQUEST_RE = re.compile(
    r"\b(?:with|showing|displaying|containing)\s+(?:readable\s+)?(?:arabic\s+)?"
    r"(?:text|words|caption|captions|title|subtitle|lettering|typography|quote|label)\b.*$"
    r"|\b(?:sign|screen|paper|note|poster)\s+(?:saying|reading|showing|displaying)\b.*$"
    r"|(?:^|\s)(?:arabic\s+)?(?:text|words|caption|captions|title|subtitle|quote|label)"
    r"\s+(?:saying|reading|showing|displaying)\b.*$",
    re.IGNORECASE,
)
_AI_IMAGE_TEXT_AVOID = (
    "readable text, captions, titles, lettering, logos, or watermarks; "
    "any interface copy must stay abstract and illegible"
)
_ACTION_FAMILY_TERMS = {
    # Treat visually interchangeable productivity props as ONE scene family.
    # This closes the real #143 failure: notebook -> sticky notes -> notebook
    # looked repetitive even though the authored search strings were different.
    "stationery": (
        "write", "writing", "written", "pen", "pencil", "notebook", "journal",
        "note", "notes", "sticky", "paper", "page", "planner", "checklist",
        "worksheet", "tasklist",
    ),
    "typing": ("type", "typing", "keyboard", "laptop", "computer"),
    "walking": ("walk", "walking", "steps", "corridor", "path"),
    "phone": ("phone", "smartphone", "screen", "notification", "scroll", "scrolling"),
    "door": ("door", "doorway", "handle", "threshold"),
    "window": ("window", "curtain", "glass"),
    "sitting": ("sit", "sitting", "chair", "desk"),
}

_QUERY_CLAUSE_BREAK_TOKENS = frozenset({"then", "while"})
_QUERY_DANGLING_TOKENS = frozenset({
    "a", "an", "the", "and", "or", "at", "by", "for", "from", "in",
    "into", "of", "on", "onto", "through", "to", "toward", "towards",
    "under", "with", "without",
})


def compact_searchable_visual_intent(
    value: object,
    *,
    drop_tokens: frozenset[str],
    max_words: int = 14,
) -> str:
    """Return a bounded search phrase without cutting an action mid-clause."""
    compact = " ".join(str(value or "").split()).strip()
    if not compact or not compact.isascii() or not any(char.isalpha() for char in compact):
        return ""
    tokens = re.findall(r"[A-Za-z0-9'-]+", compact)
    useful = [token for token in tokens if token.casefold() not in drop_tokens]
    if len(useful) < 3:
        return ""

    bounded = useful
    if len(useful) > max_words:
        bounded = useful[:max_words]
        for index, token in enumerate(bounded):
            if index >= 3 and token.casefold() in _QUERY_CLAUSE_BREAK_TOKENS:
                bounded = bounded[:index]
                break
        while bounded and bounded[-1].casefold() in _QUERY_DANGLING_TOKENS:
            bounded.pop()
    if len(bounded) < 3:
        return ""
    return " ".join(bounded)


def _visual_action_family(value: object) -> str:
    tokens = set(re.findall(r"[a-z0-9]+", str(value or "").casefold()))
    if not tokens:
        return ""
    best_name = ""
    best_score = 0
    for name, terms in _ACTION_FAMILY_TERMS.items():
        score = sum(term in tokens for term in terms)
        if score > best_score:
            best_name = name
            best_score = score
    return best_name if best_score > 0 else ""


def _default_environment_family(value: object) -> str:
    """Return one coarse scene family for continuity without creating a new stage."""
    tokens = set(re.findall(r"[a-z0-9]+", str(value or "").casefold()))
    families = (
        ("workplace", {"office", "workplace", "coworker", "meeting", "desk", "keyboard", "laptop", "computer"}),
        ("home", {"home", "kitchen", "bedroom", "living", "sofa", "house"}),
        ("transit", {"train", "bus", "car", "commute", "station", "platform", "subway"}),
        ("public_space", {"street", "city", "cafe", "store", "library", "hall", "lobby"}),
        ("outdoors", {"park", "path", "outdoor", "nature", "trail", "garden", "walking"}),
    )
    for name, markers in families:
        if tokens & markers:
            return name
    return "contextual"


def _default_shot_role(role: str, beat_in_section: int) -> str:
    if role == "hook":
        return "action"
    if role == "payoff":
        return "payoff"
    if beat_in_section <= 0:
        return "establish"
    if beat_in_section == 1:
        return "detail"
    return "consequence"


def _normalize_environment_family(value: object, fallback: object) -> str:
    raw = str(value or "").strip().casefold()
    compact = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")[:48]
    return compact or _default_environment_family(fallback)


def _strip_embedded_text_request(value: object) -> str:
    compact = " ".join(str(value or "").split()).strip()
    if not compact:
        return ""
    if not _EMBEDDED_TEXT_REQUEST_RE.search(compact):
        return compact
    return _EMBEDDED_TEXT_REQUEST_RE.sub("", compact).strip(" ,;:-")


def _writer_searchable_intent(value: object) -> str:
    """Compact one authored visual intent into a concrete provider/search boundary."""
    compact = _strip_embedded_text_request(value)
    return compact_searchable_visual_intent(
        compact,
        drop_tokens=_WRITER_INTENT_DROP_TOKENS,
    )


def _writer_beat_anchors(narration: object, count: int) -> list[str]:
    """Split final writer narration into bounded contiguous anchors for its visual beats."""
    compact = " ".join(str(narration or "").split()).strip()
    if not compact or count <= 0:
        return []
    parts = [
        " ".join(item.split()).strip()
        for item in re.split(r"(?<=[.!؟?!؛;])\s+|[،,]\s*", compact)
        if " ".join(item.split()).strip()
    ]
    if len(parts) < count:
        words = compact.split()
        if len(words) < count:
            return [compact[:320] for _ in range(count)]
        anchors = []
        for index in range(count):
            start = (index * len(words)) // count
            end = ((index + 1) * len(words)) // count
            anchors.append(" ".join(words[start:max(start + 1, end)])[:320].strip())
        return anchors

    anchors = []
    for index in range(count):
        start = (index * len(parts)) // count
        end = ((index + 1) * len(parts)) // count
        anchors.append(" ".join(parts[start:max(start + 1, end)])[:320].strip())
    return anchors


def _contains_prayer_text(value: object) -> bool:
    compact = " ".join(str(value or "").split()).strip()
    return all(marker in compact for marker in _PRAYER_TEXT_MARKERS)


def _beat_role(index: int, total: int) -> str:
    if index == 0:
        return "hook"
    if index == total - 1:
        return "payoff"
    return "body"


def _default_editorial_signals(role: str) -> dict[str, str]:
    """Meaning-led defaults for old/resumed plans that predate the signals."""
    if role == "hook":
        return {
            "hold_reason": "hook_progression",
            "pause_intent": "micro",
            "audio_energy": "steady",
        }
    if role == "payoff":
        return {
            "hold_reason": "payoff_landing",
            "pause_intent": "ending",
            "audio_energy": "resolve",
        }
    return {
        "hold_reason": "idea_changes",
        "pause_intent": "transition",
        "audio_energy": "low",
    }


def _fallback_retention_thread(plan: Mapping[str, Any]) -> dict[str, str]:
    sections = [item for item in (plan.get("sections") or []) if isinstance(item, Mapping)]
    first = sections[0] if sections else {}
    last = sections[-1] if sections else {}
    return {
        "hook_tension": str(first.get("purpose") or plan.get("promise") or "").strip(),
        "payoff_answer": str(last.get("purpose") or plan.get("promise") or "").strip(),
        "visual_motif": str(first.get("visual_query_en") or "one recurring visual object").strip(),
    }


def _fallback_stock_query(
    section: Mapping[str, Any],
    *,
    beat_in_section: int,
    shot_intent: str,
) -> str:
    candidates = (
        section.get("visual_query_en") if beat_in_section == 0 else None,
        section.get("visual_query_alt_en") if beat_in_section == 1 else None,
        shot_intent if shot_intent.isascii() else None,
        section.get("visual_query_en"),
    )
    return next((str(item).strip() for item in candidates if str(item or "").strip()), "")


def _query_key(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def fallback_visual_story(plan: Mapping[str, Any]) -> dict[str, Any]:
    sections = [item for item in (plan.get("sections") or []) if isinstance(item, Mapping)]
    if not sections:
        raise ValueError("visual story requires at least one planned section")

    purposes = [str(item.get("purpose") or "").strip() for item in sections]
    midpoint = purposes[len(purposes) // 2]
    beats = []
    for index, section in enumerate(sections, start=1):
        purpose = str(section.get("purpose") or "").strip()
        beats.append(
            {
                "id": f"b{index}",
                "section_id": str(section.get("id") or f"s{index}"),
                "viewer_intent": f"{purpose}؛ المرحلة {index}".strip("؛ "),
                "meaning_target": purpose or str(section.get("visual_query_en") or "").strip(),
                "semantic_must_have": [str(section.get("visual_query_en") or "").strip()],
                "semantic_should_avoid": [
                    "generic mood-only productivity imagery",
                    *CHANNEL_VISUAL_AVOID,
                ][:4],
                "shot_intent": str(section.get("visual_query_en") or "").strip(),
                "role": _beat_role(index - 1, len(sections)),
                "stock_query_en": str(section.get("visual_query_en") or "").strip(),
                "display_text_ar": str(section.get("cover_text") or "").strip(),
                "source_preference": "stock_motion",
                "shot_role": _default_shot_role(_beat_role(index - 1, len(sections)), 0),
                "environment_family": _default_environment_family(
                    section.get("visual_query_en") or purpose
                ),
                **_default_editorial_signals(_beat_role(index - 1, len(sections))),
            }
        )
    if len(beats) == 1:
        section = sections[0]
        base_query = str(section.get("visual_query_en") or "").strip()
        payoff_query = str(section.get("visual_query_alt_en") or "").strip()
        if not payoff_query or _query_key(payoff_query) == _query_key(base_query):
            payoff_query = (base_query[:220].rstrip() + " visibly completed outcome").strip()
        purpose = str(section.get("purpose") or "").strip()
        beats[0]["role"] = "hook"
        beats.append(
            {
                "id": "b2",
                "section_id": str(section.get("id") or "s1"),
                "viewer_intent": (purpose + "؛ تظهر النتيجة المكتسبة").strip("؛ "),
                "meaning_target": (purpose + "؛ observable completed state").strip("؛ "),
                "semantic_must_have": [payoff_query],
                "semantic_should_avoid": ["generic mood-only productivity imagery"],
                "shot_intent": (purpose[:220].rstrip() + "؛ حالة النتيجة المرئية").strip(),
                "role": "payoff",
                "stock_query_en": payoff_query,
                "display_text_ar": str(section.get("cover_text") or "").strip(),
                "source_preference": "stock_motion",
                "shot_role": "payoff",
                "environment_family": _default_environment_family(payoff_query),
                **_default_editorial_signals("payoff"),
            }
        )
    # Provider-light/local fallback must not silently regress the production to
    # stock-only. Keep motion footage dominant, but reserve two controlled
    # explanatory anchors for the unresolved opening tension and earned payoff.
    # The AI route itself remains free-only and fail-soft to audited stock.
    if len(beats) >= 2:
        beats[0]["source_preference"] = "ai_still"
        beats[-1]["source_preference"] = "ai_still"

    return {
        "schema_version": 2,
        "visual_world": VISUAL_WORLD_DEFAULT,
        "story_arc": {
            "beginning": purposes[0],
            "transformation": midpoint,
            "arrival": purposes[-1],
        },
        "retention_thread": _fallback_retention_thread(plan),
        "beats": beats,
    }


def validate_visual_story(value: Any, plan: Mapping[str, Any]) -> dict[str, Any]:
    if value is None:
        return fallback_visual_story(plan)
    if not isinstance(value, Mapping):
        raise ValueError("visual_story must be an object")

    visual_world = " ".join(str(value.get("visual_world") or "").split()).strip()
    raw_arc = value.get("story_arc")
    raw_beats = value.get("beats")
    if not visual_world or not isinstance(raw_arc, Mapping) or not isinstance(raw_beats, list):
        raise ValueError("visual_story requires visual_world, story_arc, and beats")

    arc = {
        key: " ".join(str(raw_arc.get(key) or "").split()).strip()
        for key in ("beginning", "transformation", "arrival")
    }
    if any(not text for text in arc.values()):
        raise ValueError("visual_story story_arc requires beginning, transformation, and arrival")

    raw_thread = value.get("retention_thread")
    fallback_thread = _fallback_retention_thread(plan)
    if raw_thread is None:
        thread = fallback_thread
        explicit_retention_contract = False
    elif not isinstance(raw_thread, Mapping):
        raise ValueError("visual_story retention_thread must be an object")
    else:
        thread = {
            key: (
                " ".join(str(raw_thread.get(key) or "").split()).strip()
                or fallback_thread[key]
            )
            for key in ("hook_tension", "payoff_answer", "visual_motif")
        }
        if any(len(text) > 400 for text in thread.values()):
            raise ValueError("visual_story retention_thread is too verbose")
        explicit_retention_contract = True

    sections = [item for item in (plan.get("sections") or []) if isinstance(item, Mapping)]
    section_ids = [str(item.get("id") or "").strip() for item in sections]
    section_order = {section_id: index for index, section_id in enumerate(section_ids)}
    section_by_id = {
        str(item.get("id") or "").strip(): item for item in sections
    }
    if not section_ids:
        raise ValueError("visual_story requires planned sections")
    if not 1 <= len(raw_beats) <= max(1, len(section_ids) * MAX_BEATS_PER_SECTION):
        raise ValueError("visual_story beat count is outside the bounded per-section limit")

    beats: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_queries: set[str] = set()
    seen_intents: set[str] = set()
    per_section = {section_id: 0 for section_id in section_ids}
    prior_section_index = -1
    for index, raw in enumerate(raw_beats, start=1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"visual_story beat {index} must be an object")
        beat_id = str(raw.get("id") or f"b{index}").strip()[:40]
        section_id = str(raw.get("section_id") or "").strip()
        viewer_intent = " ".join(str(raw.get("viewer_intent") or "").split()).strip()
        meaning_target = " ".join(
            str(raw.get("meaning_target") or viewer_intent or raw.get("shot_intent") or "").split()
        ).strip()
        semantic_must_have = [
            " ".join(str(item).split()).strip()[:120]
            for item in (raw.get("semantic_must_have") or [])
            if " ".join(str(item).split()).strip()
        ][:4]
        semantic_should_avoid = [
            " ".join(str(item).split()).strip()[:120]
            for item in (raw.get("semantic_should_avoid") or [])
            if " ".join(str(item).split()).strip()
        ][:4]
        for default_avoid in CHANNEL_VISUAL_AVOID:
            if default_avoid not in semantic_should_avoid and len(semantic_should_avoid) < 4:
                semantic_should_avoid.append(default_avoid)
        shot_intent = " ".join(str(raw.get("shot_intent") or "").split()).strip()
        role = (
            _beat_role(index - 1, len(raw_beats))
            if explicit_retention_contract
            else str(raw.get("role") or _beat_role(index - 1, len(raw_beats))).strip()
        )
        explicit_stock_query = "stock_query_en" in raw
        stock_query_en = " ".join(str(raw.get("stock_query_en") or "").split()).strip()
        stock_query_alt_en = " ".join(str(raw.get("stock_query_alt_en") or "").split()).strip()
        if not stock_query_en and not explicit_stock_query:
            stock_query_en = _fallback_stock_query(
                section_by_id.get(section_id) or {},
                beat_in_section=per_section.get(section_id, 0),
                shot_intent=shot_intent,
            )
        source_preference = str(
            raw.get("source_preference") or "stock_motion"
        ).strip()
        defaults = _default_editorial_signals(role)
        hold_reason = str(raw.get("hold_reason") or defaults["hold_reason"]).strip()
        pause_intent = str(raw.get("pause_intent") or defaults["pause_intent"]).strip()
        audio_energy = str(raw.get("audio_energy") or defaults["audio_energy"]).strip()
        shot_role = str(
            raw.get("shot_role") or _default_shot_role(role, per_section.get(section_id, 0))
        ).strip()
        environment_family = _normalize_environment_family(
            raw.get("environment_family"),
            shot_intent or stock_query_en,
        )
        display_text_ar = " ".join(
            str(raw.get("display_text_ar") or "").split()
        ).strip()
        writer_anchor_ar = " ".join(
            str(raw.get("writer_anchor_ar") or "").split()
        ).strip()[:320]
        if _contains_prayer_text(display_text_ar):
            raise ValueError(
                f"visual_story beat {beat_id} must not place prayer text in display_text_ar"
            )
        if not display_text_ar:
            viewer_words = viewer_intent.split()
            if (
                re.search(r"[\u0600-\u06ff]", viewer_intent)
                and 2 <= len(viewer_words) <= 10
            ):
                display_text_ar = viewer_intent
            else:
                section = section_by_id.get(section_id) or {}
                display_text_ar = " ".join(
                    str(section.get("cover_text") or "").split()
                ).strip()

        if not beat_id or beat_id in seen_ids:
            raise ValueError("visual_story beat ids must be unique and non-empty")
        if section_id not in section_order:
            raise ValueError(f"visual_story beat {beat_id} references an unknown section")
        if section_order[section_id] < prior_section_index:
            raise ValueError("visual_story beats must follow planned section order")
        if not viewer_intent or not meaning_target or not shot_intent or not stock_query_en:
            raise ValueError(
                f"visual_story beat {beat_id} requires viewer_intent, meaning_target, "
                "shot_intent, and stock_query_en"
            )
        if len(display_text_ar.split()) > 10:
            raise ValueError(
                f"visual_story beat {beat_id} display_text_ar must stay concise"
            )
        if len(viewer_intent) > 600 or len(shot_intent) > 260:
            raise ValueError(f"visual_story beat {beat_id} is too verbose")
        if len(stock_query_en) > 260:
            raise ValueError(f"visual_story beat {beat_id} stock_query_en is too verbose")
        if len(stock_query_alt_en) > 260:
            raise ValueError(f"visual_story beat {beat_id} stock_query_alt_en is too verbose")
        if re.search(r"[\u0600-\u06ff]", stock_query_en):
            raise ValueError(
                f"visual_story beat {beat_id} stock_query_en must stay English"
            )
        if stock_query_alt_en and re.search(r"[\u0600-\u06ff]", stock_query_alt_en):
            raise ValueError(
                f"visual_story beat {beat_id} stock_query_alt_en must stay English"
            )
        if stock_query_alt_en and _query_key(stock_query_alt_en) == _query_key(stock_query_en):
            stock_query_alt_en = ""
        if role not in BEAT_ROLES:
            raise ValueError(f"visual_story beat {beat_id} has invalid role")
        if source_preference not in SOURCE_PREFERENCES:
            raise ValueError(
                f"visual_story beat {beat_id} source_preference must be stock_motion, stock_still, or ai_still"
            )
        if hold_reason not in HOLD_REASONS:
            raise ValueError(f"visual_story beat {beat_id} has invalid hold_reason")
        if pause_intent not in PAUSE_INTENTS:
            raise ValueError(f"visual_story beat {beat_id} has invalid pause_intent")
        if audio_energy not in AUDIO_ENERGIES:
            raise ValueError(f"visual_story beat {beat_id} has invalid audio_energy")
        if shot_role not in SHOT_ROLES:
            raise ValueError(f"visual_story beat {beat_id} has invalid shot_role")
        if not environment_family or not environment_family.isascii():
            raise ValueError(f"visual_story beat {beat_id} has invalid environment_family")

        prior_section_index = section_order[section_id]
        per_section[section_id] += 1
        if per_section[section_id] > MAX_BEATS_PER_SECTION:
            raise ValueError(
                f"visual_story section {section_id} exceeds {MAX_BEATS_PER_SECTION} beats"
            )
        seen_ids.add(beat_id)
        query_key = _query_key(stock_query_en)
        intent_key = " ".join(viewer_intent.lower().split())
        if explicit_retention_contract and explicit_stock_query:
            if query_key in seen_queries:
                fallback_query = _fallback_stock_query(
                    section_by_id.get(section_id) or {},
                    beat_in_section=per_section[section_id] - 1,
                    shot_intent=shot_intent,
                )
                fallback_key = _query_key(fallback_query)
                if (
                    fallback_key
                    and fallback_key not in seen_queries
                    and not re.search(r"[\u0600-\u06ff]", fallback_query)
                ):
                    stock_query_en = fallback_query
                    query_key = fallback_key
            if not query_key or query_key in seen_queries:
                raise ValueError(
                    "visual_story stock_query_en values must be distinct per beat"
                )
            if intent_key in seen_intents:
                raise ValueError(
                    "visual_story viewer_intent values must add new information per beat"
                )
        seen_queries.add(query_key)
        seen_intents.add(intent_key)
        beats.append(
            {
                "id": beat_id,
                "section_id": section_id,
                "viewer_intent": viewer_intent,
                "meaning_target": meaning_target[:320],
                "semantic_must_have": semantic_must_have,
                "semantic_should_avoid": semantic_should_avoid,
                "shot_intent": shot_intent,
                "role": role,
                "stock_query_en": stock_query_en,
                **({"stock_query_alt_en": stock_query_alt_en} if stock_query_alt_en else {}),
                "display_text_ar": display_text_ar,
                "source_preference": source_preference,
                "shot_role": shot_role,
                "environment_family": environment_family,
                "hold_reason": hold_reason,
                "pause_intent": pause_intent,
                "audio_energy": audio_energy,
                **({"writer_anchor_ar": writer_anchor_ar} if writer_anchor_ar else {}),
            }
        )

    missing = [section_id for section_id, count in per_section.items() if count == 0]
    if missing:
        raise ValueError(
            "visual_story must cover every planned section: missing=" + ",".join(missing)
        )

    ai_still_count = sum(
        beat["source_preference"] == "ai_still" for beat in beats
    )
    if ai_still_count > MAX_AI_STILL_BEATS:
        raise ValueError(
            f"visual_story permits at most {MAX_AI_STILL_BEATS} ai_still anchor beats"
        )
    if explicit_retention_contract:
        if len(beats) < 2:
            raise ValueError(
                "visual_story fresh hook-to-payoff contract requires at least two beats"
            )
        if beats[0]["role"] != "hook":
            raise ValueError("visual_story first beat role must be hook")
        if beats[-1]["role"] != "payoff":
            raise ValueError("visual_story final beat role must be payoff")
        if any(beat["role"] != "body" for beat in beats[1:-1]):
            raise ValueError("visual_story middle beat roles must be body")

    return {
        "schema_version": 2,
        "visual_world": visual_world[:800],
        "story_arc": {key: text[:400] for key, text in arc.items()},
        "retention_thread": {key: text[:400] for key, text in thread.items()},
        "beats": beats,
    }



def bind_visual_story_to_script(
    visual_story: Mapping[str, Any],
    plan: Mapping[str, Any],
    script: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind Planning visuals to the accepted Writer output without another model call.

    The Writer owns the final spoken meaning. This local pass keeps Planning's visual
    semantics, gives every beat a bounded narration anchor from the final script, and
    turns vague/stylistic shot_intent text into the same concrete English boundary
    consumed by Stock/AI. It adds no stage, provider, retry loop, or quality gate.
    """
    # Planning already validated and persisted this exact story. The Writer binder
    # is a deterministic enrichment step, not a second Planning gate: re-validating
    # here would incorrectly apply fresh-output rules to compatibility/fallback stories.
    story = copy.deepcopy(dict(visual_story))
    raw_sections = [
        item for item in (script.get("sections") or []) if isinstance(item, Mapping)
    ]
    narration_by_id = {
        str(item.get("id") or "").strip(): " ".join(
            str(item.get("narration") or "").split()
        ).strip()
        for item in raw_sections
    }
    plan_sections = [
        item for item in (plan.get("sections") or []) if isinstance(item, Mapping)
    ]
    expected_ids = [
        str(item.get("id") or "").strip()
        for item in plan_sections
    ]
    section_by_id = {
        str(item.get("id") or "").strip(): item for item in plan_sections
    }
    if set(narration_by_id) != set(expected_ids) or any(
        not narration_by_id.get(section_id) for section_id in expected_ids
    ):
        raise ValueError("writer visual binding requires complete final script sections")

    beats_by_section: dict[str, list[dict[str, Any]]] = {
        section_id: [] for section_id in expected_ids
    }
    for beat in story.get("beats") or []:
        if not isinstance(beat, dict):
            continue
        section_id = str(beat.get("section_id") or "").strip()
        if section_id in beats_by_section:
            beats_by_section[section_id].append(beat)

    prior_action_family = ""
    for section_id in expected_ids:
        section_beats = beats_by_section[section_id]
        if not section_beats:
            raise ValueError(
                f"writer visual binding missing beats for section {section_id}"
            )
        anchors = _writer_beat_anchors(
            narration_by_id[section_id],
            len(section_beats),
        )
        if len(anchors) != len(section_beats):
            raise ValueError(
                f"writer visual binding could not anchor section {section_id}"
            )
        for beat, anchor in zip(section_beats, anchors):
            direct = _writer_searchable_intent(beat.get("shot_intent"))
            fallback = _writer_searchable_intent(beat.get("stock_query_en"))
            if direct or fallback:
                resolved_intent = direct or fallback
                beat["shot_intent"] = resolved_intent
                # Retrieval must follow the final observable Writer-bound intent.
                # Keeping a stale Planning query reopens the exact post-#943 drift.
                beat["stock_query_en"] = resolved_intent

            current_family = _visual_action_family(beat.get("shot_intent"))
            if current_family and current_family == prior_action_family:
                section = section_by_id.get(section_id) or {}
                alternate = _writer_searchable_intent(section.get("visual_query_alt_en"))
                alternate_family = _visual_action_family(alternate)
                if alternate and alternate_family != current_family:
                    # An unclassified alternate is still useful diversity. Requiring a
                    # second named family caused obviously different scenes (for example
                    # bookshelf/environment) to be ignored in favor of repeated stationery.
                    beat["shot_intent"] = alternate
                    beat["stock_query_en"] = alternate
                    current_family = alternate_family
                else:
                    avoids = [
                        str(item).strip()
                        for item in (beat.get("semantic_should_avoid") or [])
                        if str(item).strip()
                    ]
                    repeat_avoid = (
                        f"repeat of previous {current_family} action/composition"
                    )
                    if repeat_avoid not in avoids:
                        avoids.insert(0, repeat_avoid)
                    beat["semantic_should_avoid"] = avoids[:4]

            # The Writer may own overlay copy, but image providers never own text.
            # Remove embedded-text requests from image semantics and keep the Arabic
            # copy only in display_text_ar for the renderer-owned overlay path.
            beat["semantic_must_have"] = [
                cue for cue in (
                    _strip_embedded_text_request(item)
                    for item in (beat.get("semantic_must_have") or [])
                )
                if cue and not _EMBEDDED_TEXT_REQUEST_RE.search(cue)
            ][:4]
            avoids = [
                str(item).strip()
                for item in (beat.get("semantic_should_avoid") or [])
                if str(item).strip()
            ]
            if _AI_IMAGE_TEXT_AVOID not in avoids:
                avoids.insert(0, _AI_IMAGE_TEXT_AVOID)
            beat["semantic_should_avoid"] = avoids[:4]
            beat["writer_anchor_ar"] = anchor
            # Track the scene that will actually be searched. An unclassified but
            # genuinely different alternate must break the prior-family chain.
            prior_action_family = current_family

    return story

def _context_fragment(value: object, fallback: str, limit: int) -> str:
    text = " ".join(str(value or "").split()).strip() or fallback
    if len(text) <= limit:
        return text
    clipped = text[:limit].rsplit(" ", 1)[0].strip()
    return clipped or text[:limit].strip()


def contextual_intent(
    visual_story: Mapping[str, Any],
    beat_id: str,
    fallback_intent: str,
) -> str:
    beats = [item for item in (visual_story.get("beats") or []) if isinstance(item, Mapping)]
    current_index = next(
        (index for index, item in enumerate(beats) if str(item.get("id") or "") == beat_id),
        None,
    )
    if current_index is None:
        return str(fallback_intent or "").strip()[:300]

    current_beat = beats[current_index]
    role = str(current_beat.get("role") or "").strip() or "body"
    shot_role = str(current_beat.get("shot_role") or "").strip() or "action"
    environment_family = str(current_beat.get("environment_family") or "").strip() or "contextual"
    current_family = _visual_action_family(
        current_beat.get("shot_intent") or fallback_intent
    )
    previous_family = (
        _visual_action_family(beats[current_index - 1].get("shot_intent"))
        if current_index > 0
        else ""
    )

    # Keep all legacy context labels plus the new hook/family rule inside the
    # existing 300-char provider contract. These labels are compatibility surface.
    current = _context_fragment(
        current_beat.get("shot_intent") or fallback_intent,
        "current beat",
        20,
    )
    previous = _context_fragment(
        beats[current_index - 1].get("shot_intent") if current_index > 0 else "",
        "story opening",
        16,
    )
    following = _context_fragment(
        beats[current_index + 1].get("shot_intent")
        if current_index + 1 < len(beats)
        else "",
        "story arrival",
        13,
    )
    meaning = _context_fragment(
        current_beat.get("meaning_target")
        or current_beat.get("viewer_intent")
        or current_beat.get("shot_intent"),
        "specific",
        8,
    )
    must_have = _context_fragment(
        ", ".join(str(item) for item in (current_beat.get("semantic_must_have") or [])),
        "concrete",
        8,
    )
    should_avoid = _context_fragment(
        ", ".join(str(item) for item in (current_beat.get("semantic_should_avoid") or [])),
        "generic",
        8,
    )
    priority_rule = ""
    if role == "hook":
        priority_rule = "Hook tension observable. "
    elif current_family and current_family == previous_family:
        priority_rule = "Repeat needs changed state. "
    tail = " Judge specific meaning before mood. Same hook-to-payoff arc: judge continuity."
    # The provider contract is capped at 300 characters. Keep editor metadata
    # compact while preserving the established family + semantic + neighbor
    # labels that downstream QA/recovery contracts already consume.
    head = (
        f"Role:{role} Shot:{shot_role[:7]} Env:{environment_family[:8]} "
        f"Fam:{current_family or 'other'}. {priority_rule}"
        f"Meaning: {meaning}. Must show: {must_have}. Avoid: {should_avoid}. "
        f"Current: {current}. Previous: {previous}. Next: {following}."
    )
    head_limit = max(0, 300 - len(tail))
    return head[:head_limit].rstrip() + tail
