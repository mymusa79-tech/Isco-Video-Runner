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

# Run117: a still photograph cannot prove actual page-turning motion. Keep
# this narrowly scoped to observable motion, not generic reading/writing poses.
_PAGE_TURNING_MOTION_RE = re.compile(
    r"\b(?:turning|flipping)\s+(?:(?:over|through|the|a|book|its|some)\s+){0,3}pages?\b",
    re.IGNORECASE,
)


def stock_scene_requires_motion(value: object) -> bool:
    """Detect an explicit temporal page-turning action in the approved shot."""
    return bool(_PAGE_TURNING_MOTION_RE.search(str(value or "")))
BEAT_ROLES = frozenset({"hook", "body", "payoff"})
MAX_BEATS_PER_SECTION = 3
MAX_AI_STILL_BEATS = 4
SHORT_BEAT_SECTION_IDS = ("s1", "s1", "s1", "s2", "s2", "s3", "s3")

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
    r"|\b(?:sign|screen|paper|note|poster)\s+(?:saying|reading)\b.*$"
    r"|\b(?:sign|screen|paper|note|poster)\s+(?:showing|displaying)\s+"
    r"(?:readable\s+)?(?:arabic\s+)?(?:text|words|caption|captions|title|subtitle|lettering|typography|quote|label)\b.*$"
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
_ACTION_FAMILY_MAX_USES = 2
PLANNING_VISUAL_FAMILY_NAMES = frozenset(_ACTION_FAMILY_TERMS)
MAX_PLANNING_REPAIR_BEATS = 60
PLANNING_QUERY_FIELDS = ("stock_query_en", "stock_query_alt_en")

_FACE_DEPENDENT_SEMANTIC_RE = re.compile(
    r"\b(?:face|facial|expression|expressions|smile|smiling|grin|grinning)\b|(?:وجه|ملامح|تعبير(?:ات)?|ابتسام\w*)",
    re.IGNORECASE,
)
_SHORT_PAYOFF_PROCESS_RE = re.compile(
    r"\b(?:write|writing|mark|marking|checklist|to-?do|task\s+list|"
    r"plan|planning|start(?:ing)?\s+to\s+write|next\s+priority)\b",
    re.IGNORECASE,
)

# A strong opening should not collapse into generic productivity B-roll once the
# episode moves into explanation. These props are allowed when the visible action
# itself proves the idea, but not when they merely host weak scrolling/typing/using.
_GENERIC_PRODUCTIVITY_PROP_TERMS = frozenset({
    "laptop", "computer", "keyboard", "desk", "phone", "smartphone", "screen",
    "tabs", "notebook", "journal", "planner", "sticky", "notes", "paper",
})
_WEAK_GENERIC_ACTION_TERMS = frozenset({
    "scroll", "scrolling", "type", "typing", "sit", "sitting", "look", "looking",
    "work", "working", "use", "using", "hold", "holding", "browse", "browsing",
})
_STRONG_SEMANTIC_ACTION_RE = re.compile(
    r"\b(?:compare|comparing|comparison|choose|choosing|choice|select|selecting|selected|"
    r"reject|rejecting|rejected|eliminate|eliminating|remove|removing|close|closing|closed|"
    r"cross|crossing|sort|sorting|separate|separating|switch|switching|rank|ranking|"
    r"narrow|narrowing|discard|discarding|reduce|reducing|arrange|arranging|mark|marking|"
    r"check|checking|complete|completed|finish|finished|pick|picking|conflict|conflicting|"
    r"unequal|different|contrast|contrasting|unfinished|blocked|interrupted)\b",
    re.IGNORECASE,
)

VISUAL_WORLD_DARK_MARKERS = (
    "navy", "dark blue", "deep blue", "charcoal", "slate",
    "dark shadow", "deep shadow", "كحلي", "أزرق داكن", "ازرق داكن", "فحمي", "فحمية",
)
VISUAL_WORLD_GOLD_MARKERS = (
    "gold", "golden", "warm gold", "gold accent", "golden accent", "amber accent",
    "ذهبي", "ذهبية", "لمسة ذهبية", "لمسات ذهبية",
)


class VisualWorldIdentityError(ValueError):
    pass


class VisualFamilyRepeatError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        family: str,
        beat_id: str,
        section_id: str,
        query: str,
    ) -> None:
        super().__init__(message)
        self.family = family
        self.beat_id = beat_id
        self.section_id = section_id
        self.query = query


def visual_world_identity_report(value: object) -> dict[str, object]:
    compact = " ".join(str(value or "").split()).casefold()
    dark_hits = [marker for marker in VISUAL_WORLD_DARK_MARKERS if marker.casefold() in compact]
    gold_hits = [marker for marker in VISUAL_WORLD_GOLD_MARKERS if marker.casefold() in compact]
    return {
        "dark_identity_present": bool(dark_hits),
        "gold_identity_present": bool(gold_hits),
        "dark_matches": dark_hits[:4],
        "gold_matches": gold_hits[:4],
    }


def require_channel_visual_world(value: object) -> str:
    compact = " ".join(str(value or "").split()).strip()
    report = visual_world_identity_report(compact)
    missing = []
    if not report["dark_identity_present"]:
        missing.append("navy/dark-blue/charcoal")
    if not report["gold_identity_present"]:
        missing.append("gold/golden-accent")
    if missing:
        raise VisualWorldIdentityError(
            "visual_world_identity_missing " + ",".join(missing)
        )
    return compact


def visual_action_family(value: object) -> str:
    return _visual_action_family(value)
_SEMANTIC_PROOF_NOISE = frozenset({
    "cinematic", "lighting", "light", "lights", "warm", "cool", "dark", "bright",
    "navy", "charcoal", "gold", "golden", "ivory", "shadow", "shadows", "highlight",
    "highlights", "contrast", "depth", "soft", "natural", "premium", "frame",
    "framing", "composition", "camera", "close", "wide", "medium", "shot", "mood",
    "atmosphere", "tone", "color", "colour", "bokeh", "glow",
})

_QUERY_CLAUSE_BREAK_TOKENS = frozenset({"then", "while", "and", "showing", "displaying"})
_QUERY_DANGLING_TOKENS = frozenset({
    "a", "an", "the", "and", "or", "at", "by", "for", "from", "in",
    "into", "of", "on", "onto", "through", "to", "toward", "towards",
    "under", "with", "without", "showing", "displaying", "holding", "while", "then",
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
        # A count cut can strand a modifier ("half-empty") with no noun.
        while bounded and (
            bounded[-1].casefold() in _QUERY_DANGLING_TOKENS or "-" in bounded[-1]
        ):
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


def _beat_action_family(beat: Mapping[str, Any]) -> str:
    """Classify the actual searchable scene, not only one descriptive field."""
    return _visual_action_family(
        " ".join(
            str(beat.get(key) or "")
            for key in ("shot_intent", "stock_query_en")
        )
    )


def _is_weak_generic_productivity_scene(value: object) -> bool:
    """Flag prop-led B-roll that does not visibly carry the episode's meaning."""
    compact = " ".join(str(value or "").split()).strip()
    if not compact or not compact.isascii():
        return False
    tokens = set(re.findall(r"[a-z0-9]+", compact.casefold()))
    if not (tokens & _GENERIC_PRODUCTIVITY_PROP_TERMS):
        return False
    if _STRONG_SEMANTIC_ACTION_RE.search(compact):
        return False
    # A generic prop with either a weak stock action or no meaningful action at
    # all is exactly the post-hook drop seen in Run 58 ("scrolling many tabs").
    return bool(tokens & _WEAK_GENERIC_ACTION_TERMS) or len(tokens) <= 10


def _is_face_dependent_semantic_cue(value: object) -> bool:
    """True when a must-have asks QA to prove meaning from a face/expression."""
    return bool(_FACE_DEPENDENT_SEMANTIC_RE.search(str(value or "")))


def _short_payoff_repeats_process_action(value: object) -> bool:
    """Keep a Short payoff on the visible result, not another planning/writing step."""
    return bool(_SHORT_PAYOFF_PROCESS_RE.search(str(value or "")))


def _has_semantic_proof(cues: list[str]) -> bool:
    """Reject must-have lists that contain only grade/composition/mood vocabulary."""
    for cue in cues:
        compact = " ".join(str(cue or "").split()).strip()
        if not compact:
            continue
        if re.search(r"[\u0600-\u06ff]", compact):
            if len(compact.split()) >= 2:
                return True
            continue
        tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", compact.casefold())
            if token not in _SEMANTIC_PROOF_NOISE
        ]
        if len(tokens) >= 2:
            return True
    return False


_MEANING_PRESERVATION_NOISE = _SEMANTIC_PROOF_NOISE | frozenset({
    "hand", "hands", "person", "people", "object", "objects", "only", "face",
    "view", "back", "scene", "workspace", "room", "table", "one", "single",
    "small", "several", "many",
})


def _meaning_preservation_tokens(value: object) -> set[str]:
    tokens: set[str] = set()
    for raw in re.findall(r"[a-z0-9]+", str(value or "").casefold()):
        if raw in _MEANING_PRESERVATION_NOISE or len(raw) < 3:
            continue
        token = raw
        if len(token) > 6 and token.endswith("ing"):
            token = token[:-3]
        elif len(token) > 5 and token.endswith("ed"):
            token = token[:-2]
        elif len(token) > 4 and token.endswith("s"):
            token = token[:-1]
        if token and token not in _MEANING_PRESERVATION_NOISE:
            tokens.add(token)
    return tokens


_MEANING_SENSITIVE_STEMS = frozenset({
    "writ", "select", "choos", "compar", "mark", "complet", "finish",
    "sort", "rank", "narrow",
})


def _alternate_preserves_beat_meaning(beat: Mapping[str, Any], alternate: str) -> bool:
    """Protect explicit semantic actions without rejecting valid authored scene changes."""
    authored = " ".join(
        str(item or "")
        for item in (beat.get("semantic_must_have") or [])
        if str(item or "").strip()
    )
    if not authored:
        authored = str(beat.get("shot_intent") or beat.get("stock_query_en") or "")
    required = _meaning_preservation_tokens(authored)
    sensitive = required & _MEANING_SENSITIVE_STEMS
    if not sensitive:
        # Existing authored alternates may intentionally change scene family to
        # express a broader beat meaning. Keep that proven behavior.
        return True
    candidate = _meaning_preservation_tokens(alternate)
    return bool(candidate and (required & candidate))


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


# Face-expression requirements are incompatible with the channel's no-clear-face
# policy. Fix only the search representation, not the authored narration/proof.
# Run 115 asked stock for "person starting to write ... focused expression"
# and then blocked a face in the exact stock shot that query solicited.
_FACE_MOOD_QUERY_RE = re.compile(
    r"\s+with\s+(?:(?:a|an)\s+)?"
    r"(?:(?:focused|thoughtful|happy|sad|worried|frustrated|calm|"
    r"satisfied|determined|confident|pleased|smiling|slight|subtle)\s+){1,3}"
    r"(?:facial\s+)?(?:expression|smile|grin)\b",
    re.IGNORECASE,
)
_HAND_ACTION_PERSON_QUERY_RE = re.compile(
    r"\b(?:(?:a|the)\s+)?(?:person|man|woman|someone|student)\s+"
    r"(?=(?:(?:starting|beginning)\s+to\s+)?"
    r"(?:write|writing|hold|holding|mark|marking|turn|turning|"
    r"place|placing|sort|sorting|draw|drawing)\b)",
    re.IGNORECASE,
)


def _face_safe_stock_intent(value: object) -> str:
    """Resolve an avoidable face/search contradiction without a new AI call."""
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    explicit_face_mood = bool(_FACE_MOOD_QUERY_RE.search(text))
    text = _FACE_MOOD_QUERY_RE.sub("", text)
    # An ordinary person writing is not itself a facial-expression demand.
    # Preserve existing stock queries unless the same cue explicitly requests
    # a visible face emotion, as it did in Run 115.
    if explicit_face_mood:
        text, hand_action_rewritten = _HAND_ACTION_PERSON_QUERY_RE.subn("hands ", text)
        if hand_action_rewritten and not re.search(r"\bno\s+face\b", text, re.IGNORECASE):
            text += " no face visible"
    return " ".join(text.split())



def _face_safe_resumed_visual_story(value: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """Refresh stale face-dependent stock searches after a voice-only resume.

    Cached Script/TTS are immutable. The normal Writer binder is skipped on a
    voice checkpoint, so newly corrected search rules must be applied locally
    to cached beats before fresh visual acquisition. This changes search text
    only; narration, approved proof, pacing and scene count stay untouched.
    """
    result = copy.deepcopy(dict(value))
    beats = result.get("beats")
    if not isinstance(beats, list):
        return result, False
    changed = False
    for beat in beats:
        if not isinstance(beat, dict):
            continue
        for key in ("shot_intent", "stock_query_en", "stock_query_alt_en"):
            authored = beat.get(key)
            if not isinstance(authored, str) or not authored:
                continue
            safe = _face_safe_stock_intent(authored)
            if safe and safe != authored:
                beat[key] = safe
                changed = True
    return result, changed


def _writer_searchable_intent(value: object) -> str:
    """Compact a face-safe, concrete intent using the existing stock boundary."""
    compact = _face_safe_stock_intent(_strip_embedded_text_request(value))
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


def _query_has_arabic(value: str) -> bool:
    return bool(re.search(r"[\u0600-\u06ff]", value))


def _viewer_intent_key(value: str) -> str:
    return " ".join(value.lower().split())


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


def _stronger_post_hook_query(query: str, alternate: str, section_alternate: str) -> str:
    return next(
        (
            candidate
            for candidate in (
                query if not _is_weak_generic_productivity_scene(query) else "",
                alternate,
                section_alternate,
            )
            if candidate
            and candidate.isascii()
            and not _is_weak_generic_productivity_scene(candidate)
        ),
        "",
    )


def visual_story_repair_context(value: Any, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Describe concurrent visual conflicts using the production scene classifiers.

    This is correction feedback, never a second validator or an acceptance path.
    Only bounded beat IDs and host-owned family/query-field names leave it.
    """
    if not isinstance(value, Mapping) or not isinstance(value.get("beats"), list):
        return {}
    sections = [item for item in (plan.get("sections") or []) if isinstance(item, Mapping)]
    section_order = {str(item.get("id") or ""): index for index, item in enumerate(sections)}
    section_by_id = {str(item.get("id") or ""): item for item in sections}
    section_beats: dict[str, list[str]] = {section_id: [] for section_id in section_order}
    family_ids: dict[str, list[str]] = {}
    weak_ids: list[str] = []
    neighbors: list[list[str]] = []
    non_english_queries: dict[str, list[str]] = {}
    duplicate_intents: list[list[str]] = []
    seen_intents: dict[str, str] = {}
    explicit_retention_contract = isinstance(value.get("retention_thread"), Mapping)
    prior_family = prior_id = ""
    resolved: list[dict[str, str]] = []
    for raw in value["beats"][:MAX_PLANNING_REPAIR_BEATS]:
        if not isinstance(raw, Mapping):
            continue
        beat_id = str(raw.get("id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", beat_id):
            continue
        section_id = str(raw.get("section_id") or "").strip()
        for field in PLANNING_QUERY_FIELDS:
            if _query_has_arabic(str(raw.get(field) or "")):
                non_english_queries.setdefault(field, []).append(beat_id)
        intent_key = _viewer_intent_key(str(raw.get("viewer_intent") or ""))
        if intent_key:
            if explicit_retention_contract and "stock_query_en" in raw and intent_key in seen_intents:
                duplicate_intents.append([seen_intents[intent_key], beat_id])
            seen_intents.setdefault(intent_key, beat_id)
        shot = " ".join(str(raw.get("shot_intent") or "").split())
        query = " ".join(str(raw.get("stock_query_en") or "").split())
        if (
            str(plan.get("_visual_semantic_strength_contract") or "") == "v1_post_hook"
            and section_order.get(section_id, 0) > 0
            and _is_weak_generic_productivity_scene(shot or query)
        ):
            stronger = _stronger_post_hook_query(
                query,
                " ".join(str(raw.get("stock_query_alt_en") or "").split()),
                " ".join(str((section_by_id.get(section_id) or {}).get("visual_query_alt_en") or "").split()),
            )
            if stronger:
                shot = query = stronger
            else:
                weak_ids.append(beat_id)
        if section_id in section_beats:
            section_beats[section_id].append(beat_id)
        beat = {"id": beat_id, "section_id": section_id, "shot_intent": shot, "stock_query_en": query}
        resolved.append(beat)
        family = _beat_action_family(beat)
        if family:
            family_ids.setdefault(family, []).append(beat_id)
            if family == prior_family:
                neighbors.append([prior_id, beat_id])
        prior_family, prior_id = family, beat_id
    context: dict[str, Any] = {}
    if any(not ids or len(ids) > MAX_BEATS_PER_SECTION for ids in section_beats.values()):
        context["section_beat_ids"] = section_beats
        context["section_beat_limit"] = MAX_BEATS_PER_SECTION
    short_contract = str(plan.get("_short_visual_diversity_contract") or "") == "v1_max2"
    if (
        short_contract
        and tuple(section_order) == ("s1", "s2", "s3")
        and len(value["beats"]) == len(resolved) == len(SHORT_BEAT_SECTION_IDS)
        and len({beat["id"] for beat in resolved}) == len(resolved)
    ):
        expected = {
            beat["id"]: section_id
            for beat, section_id in zip(resolved, SHORT_BEAT_SECTION_IDS)
        }
        misplaced = {
            beat["id"]: expected[beat["id"]] for beat in resolved
            if beat["section_id"] != expected[beat["id"]]
        }
        if misplaced:
            context["short_beat_section_ids"] = expected
            context["misplaced_section_beat_ids"] = misplaced
    if non_english_queries:
        context["non_english_query_beat_ids"] = non_english_queries
    if duplicate_intents:
        context["duplicate_viewer_intent_pairs"] = duplicate_intents
    if weak_ids:
        context["post_hook_weak_beat_ids"] = weak_ids
    if family_ids:
        context["family_beat_ids"] = family_ids
    if neighbors:
        context["same_family_neighbors"] = neighbors
    if short_contract:
        context["visual_family_limit"] = _ACTION_FAMILY_MAX_USES
        overused = [family for family, ids in family_ids.items() if len(ids) > _ACTION_FAMILY_MAX_USES]
        if overused:
            context["overused_families"] = overused
        if (
            resolved
            and _beat_action_family(resolved[-1]) == "stationery"
            and len(family_ids.get("stationery", [])) > 1
            and _short_payoff_repeats_process_action(resolved[-1]["shot_intent"] or resolved[-1]["stock_query_en"])
        ):
            context["payoff_process_beat_id"] = resolved[-1]["id"]
    # Keep the earliest permitted nonadjacent uses and name scenes to replace.
    # This is feedback only: no authored meaning, query or section is mutated.
    replacements: dict[str, list[str]] = {}
    kept: dict[str, list[int]] = {}
    for index, beat in enumerate(resolved):
        family = _beat_action_family(beat)
        if not family:
            continue
        indexes = kept.setdefault(family, [])
        if (
            (short_contract and len(indexes) >= _ACTION_FAMILY_MAX_USES)
            or (indexes and indexes[-1] == index - 1)
            or beat["id"] == context.get("payoff_process_beat_id")
        ):
            replacements.setdefault(family, []).append(beat["id"])
        else:
            indexes.append(index)
    if replacements:
        context["family_replacement_beat_ids"] = replacements
    return context


def repair_short_family_overuse(value: Any, plan: Mapping[str, Any]) -> Any:
    """Host-side fix for a provider story that repeats one visual action family.

    A Short beat beyond the allowed uses of its family is switched to the same
    section's own alternate query (written by the provider) when that alternate
    is a different family and a distinct query. Nothing is invented: if no
    alternate qualifies the story is returned unchanged and the validator
    rejects it exactly as before.
    """
    if not isinstance(value, Mapping) or not isinstance(value.get("beats"), list):
        return value
    beats = [dict(b) if isinstance(b, Mapping) else b for b in value["beats"]]
    if not all(isinstance(b, dict) for b in beats):
        return value
    sections = {
        str(item.get("id") or ""): item
        for item in (plan.get("sections") or [])
        if isinstance(item, Mapping)
    }
    changed = False
    for _ in range(len(beats)):
        uses: dict[str, int] = {}
        offender = -1
        offender_family = ""
        for index, beat in enumerate(beats):
            family = _beat_action_family(beat)
            if not family:
                continue
            uses[family] = uses.get(family, 0) + 1
            if uses[family] > _ACTION_FAMILY_MAX_USES:
                offender, offender_family = index, family
                break
        if offender < 0:
            break
        beat = beats[offender]
        section = sections.get(str(beat.get("section_id") or ""), {})
        used_keys = {_query_key(str(b.get("stock_query_en") or "")) for b in beats}
        replacement = ""
        for candidate in (section.get("visual_query_alt_en"), section.get("visual_query_en")):
            text = " ".join(str(candidate or "").split())
            if not text or not text.isascii() or _query_key(text) in used_keys:
                continue
            family = _visual_action_family(text)
            if family == offender_family or (family and uses.get(family, 0) >= _ACTION_FAMILY_MAX_USES):
                continue
            replacement = text
            break
        if not replacement:
            return value if not changed else {**value, "beats": beats}
        beat["stock_query_en"] = replacement
        beat["shot_intent"] = replacement
        beat["semantic_must_have"] = [replacement]
        beat.pop("stock_query_alt_en", None)
        changed = True
    return {**value, "beats": beats} if changed else value


def validate_visual_story(value: Any, plan: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return _validate_visual_story(value, plan)
    except ValueError as exc:
        # The gate still raises its exact original rejection. Report the other
        # visible conflicts too so its bounded provider correction fixes them
        # together instead of discovering one new rejection per provider call.
        context = dict(getattr(exc, "planning_repair_context", {}) or {})
        context.update(visual_story_repair_context(value, plan))
        if context:
            exc.planning_repair_context = context
        raise


def _validate_visual_story(value: Any, plan: Mapping[str, Any]) -> dict[str, Any]:
    if value is None:
        return fallback_visual_story(plan)
    if not isinstance(value, Mapping):
        raise ValueError("visual_story must be an object")

    visual_world = " ".join(str(value.get("visual_world") or "").split()).strip()
    raw_arc = value.get("story_arc")
    raw_beats = value.get("beats")
    if not visual_world or not isinstance(raw_arc, Mapping) or not isinstance(raw_beats, list):
        raise ValueError("visual_story requires visual_world, story_arc, and beats")
    if str(plan.get("_visual_identity_contract") or "") == "navy_gold_v1":
        visual_world = require_channel_visual_world(visual_world)

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
    seen_queries: dict[str, str] = {}
    seen_intents: dict[str, str] = {}
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
        if str(plan.get("_visual_no_face_semantic_contract") or "") == "v1":
            semantic_must_have = [
                cue for cue in semantic_must_have
                if not _is_face_dependent_semantic_cue(cue)
            ]
        semantic_should_avoid = [
            " ".join(str(item).split()).strip()[:120]
            for item in (raw.get("semantic_should_avoid") or [])
            if " ".join(str(item).split()).strip()
        ][:4]
        for default_avoid in CHANNEL_VISUAL_AVOID:
            if default_avoid not in semantic_should_avoid and len(semantic_should_avoid) < 4:
                semantic_should_avoid.append(default_avoid)
        shot_intent = " ".join(str(raw.get("shot_intent") or "").split()).strip()
        if not semantic_must_have and shot_intent:
            semantic_must_have = [shot_intent[:120]]
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
        if explicit_retention_contract and not _has_semantic_proof(semantic_must_have):
            raise ValueError(
                f"visual_story beat {beat_id} semantic_must_have must contain observable "
                "semantic evidence, not only mood/lighting/composition"
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
        if _query_has_arabic(stock_query_en):
            raise ValueError(
                f"visual_story beat {beat_id} stock_query_en must stay English"
            )
        if stock_query_alt_en and _query_has_arabic(stock_query_alt_en):
            raise ValueError(
                f"visual_story beat {beat_id} stock_query_alt_en must stay English"
            )
        if stock_query_alt_en and _query_key(stock_query_alt_en) == _query_key(stock_query_en):
            stock_query_alt_en = ""

        # Shared post-hook visual floor for Short, Film, and Podcast. A strong
        # opener must not be followed by generic laptop/phone/desk coverage that
        # merely scrolls, types, works, or looks. Prefer an already-authored
        # semantically stronger alternate locally; otherwise reject the fresh plan
        # before any media retrieval so quality cannot silently drop downstream.
        if (
            str(plan.get("_visual_semantic_strength_contract") or "") == "v1_post_hook"
            and section_order.get(section_id, 0) > 0
            and _is_weak_generic_productivity_scene(shot_intent or stock_query_en)
        ):
            section_alt = " ".join(
                str((section_by_id.get(section_id) or {}).get("visual_query_alt_en") or "").split()
            ).strip()
            stronger = _stronger_post_hook_query(stock_query_en, stock_query_alt_en, section_alt)
            if not stronger:
                raise ValueError(
                    f"visual_story beat {beat_id} post-hook semantic drop requires "
                    "a stronger observable alternate"
                )
            shot_intent = stronger
            stock_query_en = stronger
            # The stronger authored alternate now owns the visible proof as well;
            # do not leave QA anchored to the rejected generic prop scene.
            semantic_must_have = [stronger[:120]]
            if stock_query_alt_en and _query_key(stock_query_alt_en) == _query_key(stronger):
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
        intent_key = _viewer_intent_key(viewer_intent)
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
                    and not _query_has_arabic(fallback_query)
                ):
                    stock_query_en = fallback_query
                    query_key = fallback_key
            if not query_key or query_key in seen_queries:
                error = ValueError(
                    "visual_story stock_query_en values must be distinct per beat"
                )
                error.planning_repair_context = {
                    "beat_id": beat_id,
                    "conflicting_beat_id": seen_queries.get(query_key, ""),
                }
                raise error
            if intent_key in seen_intents:
                error = ValueError(
                    "visual_story viewer_intent values must add new information per beat"
                )
                error.planning_repair_context = {
                    "beat_id": beat_id,
                    "conflicting_beat_id": seen_intents[intent_key],
                }
                raise error
        seen_queries[query_key] = beat_id
        seen_intents.setdefault(intent_key, beat_id)
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

    # Fresh Short plans fail early when one action family dominates the story.
    # This keeps a provider from passing Planning with seven distinct query strings
    # that are still visually the same stationery/writing scene.
    if str(plan.get("_short_visual_diversity_contract") or "") == "v1_max2":
        family_uses: dict[str, int] = {}
        for beat in beats:
            family = _beat_action_family(beat)
            if family:
                family_uses[family] = family_uses.get(family, 0) + 1
                if family_uses[family] > _ACTION_FAMILY_MAX_USES:
                    raise ValueError(
                        "visual_story Short visual family exceeds two beats: "
                        f"{family}"
                    )

        payoff = beats[-1] if beats else {}
        payoff_family = _beat_action_family(payoff)
        earlier_same_family = any(
            _beat_action_family(beat) == payoff_family
            for beat in beats[:-1]
        ) if payoff_family else False
        if (
            payoff_family == "stationery"
            and earlier_same_family
            and _short_payoff_repeats_process_action(
                payoff.get("shot_intent") or payoff.get("stock_query_en")
            )
        ):
            raise ValueError(
                "visual_story Short payoff must show the visible result/state, "
                "not another writing/planning/checklist action"
            )

    # Visual-family repetition is also enforced after Writer binding, immediately
    # before retrieval, so legacy/compatibility stories can still be normalized
    # without weakening the production gate.
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
    *,
    allow_composition_fallback: bool = False,
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
    family_uses: dict[str, int] = {}
    strict_diversity = (
        str(plan.get("_visual_diversity_contract") or "") == "v2_fail_closed"
        or any(
            str(item.get("visual_query_alt_en") or "").strip()
            for item in plan_sections
        )
        or any(
            str(item.get("stock_query_alt_en") or "").strip()
            for item in (story.get("beats") or [])
            if isinstance(item, Mapping)
        )
    )
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
            # The bounded recovery query must obey the same face-safe search
            # boundary as the primary query; do not rewrite unrelated alternates.
            raw_alternate = str(beat.get("stock_query_alt_en") or "")
            safe_alternate = _face_safe_stock_intent(raw_alternate)
            if safe_alternate != raw_alternate and safe_alternate:
                beat["stock_query_alt_en"] = safe_alternate
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
                alternates = (
                    _writer_searchable_intent(beat.get("stock_query_alt_en")),
                    _writer_searchable_intent(section.get("visual_query_alt_en")),
                )
                replacement = ""
                replacement_family = ""
                for alternate in alternates:
                    alternate_family = _visual_action_family(alternate)
                    if (
                        alternate
                        and alternate_family != current_family
                        and _alternate_preserves_beat_meaning(beat, alternate)
                    ):
                        replacement = alternate
                        replacement_family = alternate_family
                        break
                if replacement:
                    # An unclassified alternate is still useful diversity. Requiring a
                    # second named family caused obviously different scenes (for example
                    # bookshelf/environment) to be ignored in favor of repeated stationery.
                    beat["shot_intent"] = replacement
                    beat["stock_query_en"] = replacement
                    # Preserve authored semantic proof: the alternate changes only
                    # how the same beat is shown, never what QA must prove.
                    current_family = replacement_family
                elif strict_diversity and not allow_composition_fallback:
                    raise VisualFamilyRepeatError(
                        "writer visual binding would repeat the previous "
                        f"{current_family} scene family without a distinct alternate",
                        family=current_family,
                        beat_id=str(beat.get("id") or ""),
                        section_id=section_id,
                        query=str(beat.get("shot_intent") or beat.get("stock_query_en") or ""),
                    )
                else:
                    avoids = [
                        str(item).strip()
                        for item in (beat.get("semantic_should_avoid") or [])
                        if str(item).strip()
                    ]
                    repeat_avoid = (
                        f"repeat of previous {current_family} action/composition; "
                        "require visibly different scale, framing, or state"
                    )
                    if repeat_avoid not in avoids:
                        avoids.insert(0, repeat_avoid)
                    beat["semantic_should_avoid"] = avoids[:4]

            if strict_diversity and current_family:
                family_uses[current_family] = family_uses.get(current_family, 0) + 1
                if family_uses[current_family] > _ACTION_FAMILY_MAX_USES:
                    if not allow_composition_fallback:
                        raise VisualFamilyRepeatError(
                            "writer visual binding repeats visual family too often: "
                            f"{current_family}",
                            family=current_family,
                            beat_id=str(beat.get("id") or ""),
                            section_id=section_id,
                            query=str(beat.get("shot_intent") or beat.get("stock_query_en") or ""),
                        )
                    avoids = [
                        str(item).strip()
                        for item in (beat.get("semantic_should_avoid") or [])
                        if str(item).strip()
                    ]
                    overuse_avoid = (
                        f"additional {current_family} repetition; require a visibly different "
                        "environment, scale, framing, or state while preserving the beat meaning"
                    )
                    if overuse_avoid not in avoids:
                        avoids.insert(0, overuse_avoid)
                    beat["semantic_should_avoid"] = avoids[:4]

            # The Writer may own overlay copy, but image providers never own text.
            # Remove embedded-text requests from image semantics and keep the Arabic
            # copy only in display_text_ar for the renderer-owned overlay path.
            beat["semantic_must_have"] = [
                cue for cue in (
                    _strip_embedded_text_request(item)
                    for item in (beat.get("semantic_must_have") or [])
                )
                if (
                    cue
                    and not _EMBEDDED_TEXT_REQUEST_RE.search(cue)
                    and not _is_face_dependent_semantic_cue(cue)
                )
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
    """Build a <=300-char Visual QA brief with semantic evidence first."""
    beats = [item for item in (visual_story.get("beats") or []) if isinstance(item, Mapping)]
    current_index = next(
        (index for index, item in enumerate(beats) if str(item.get("id") or "") == beat_id),
        None,
    )
    if current_index is None:
        return str(fallback_intent or "").strip()[:300]

    current_beat = beats[current_index]
    role = str(current_beat.get("role") or "").strip() or "body"
    current_family = _beat_action_family(current_beat)
    previous_family = (
        _beat_action_family(beats[current_index - 1])
        if current_index > 0
        else ""
    )
    meaning = _context_fragment(
        current_beat.get("meaning_target")
        or current_beat.get("viewer_intent")
        or current_beat.get("shot_intent"),
        "specific visible meaning",
        44,
    )
    raw_must = [
        str(item).strip()
        for item in (current_beat.get("semantic_must_have") or [])
        if str(item).strip()
    ]
    raw_avoid = [
        str(item).strip()
        for item in (current_beat.get("semantic_should_avoid") or [])
        if str(item).strip()
    ]
    must_have = _context_fragment(
        ", ".join(raw_must) or current_beat.get("shot_intent"),
        "visible proof",
        34,
    )
    avoid = _context_fragment(", ".join(raw_avoid), "", 16) if raw_avoid else ""
    current = _context_fragment(
        current_beat.get("shot_intent") or fallback_intent,
        "current",
        22,
    )
    previous = _context_fragment(
        beats[current_index - 1].get("shot_intent") if current_index > 0 else "",
        "story opening",
        20,
    )
    following = _context_fragment(
        beats[current_index + 1].get("shot_intent")
        if current_index + 1 < len(beats)
        else "",
        "story arrival",
        20,
    )

    # Meaning/proof always come before family, mood and continuity metadata.
    pieces = [
        f"Role:{role}",
        f"Meaning:{meaning}",
        f"Must show:{must_have}",
    ]
    if avoid:
        pieces.append(f"Avoid:{avoid}")
    if current_family:
        pieces.append(f"Fam:{current_family}")
    if previous_family:
        pieces.append(f"PrevFam:{previous_family}")
    if current_family and previous_family == current_family:
        pieces.append("Repeat: changed state only")
    pieces.extend(
        [
            f"Current: {current}",
            f"Previous: {previous}",
            f"Next: {following}",
        ]
    )
    if current_beat.get("writer_anchor_ar"):
        pieces.append(
            f"Narration:{_context_fragment(current_beat.get('writer_anchor_ar'), 'spoken', 24)}"
        )
    if role == "hook":
        pieces.append("Hook: unresolved observable tension")

    tail = " Judge specific meaning before mood. Same hook-to-payoff arc: judge continuity."
    head_limit = 300 - len(tail)
    head = ". ".join(pieces) + "."
    if len(head) > head_limit:
        # Preserve semantic evidence and all continuity labels; drop only optional
        # narration first, then shorten family/repeat metadata by omission.
        essential = [
            f"Role:{role}",
            f"Meaning:{meaning}",
            f"Must show:{must_have}",
        ]
        if avoid:
            essential.append(f"Avoid:{avoid}")
        essential.extend(
            [
                f"Current: {_context_fragment(current_beat.get('shot_intent') or fallback_intent, 'current', 16)}",
                f"Previous: {_context_fragment(beats[current_index - 1].get('shot_intent') if current_index > 0 else '', 'story opening', 16)}",
                f"Next: {_context_fragment(beats[current_index + 1].get('shot_intent') if current_index + 1 < len(beats) else '', 'story arrival', 16)}",
            ]
        )
        if current_family:
            essential.append(f"Fam:{current_family}")
        if previous_family:
            essential.append(f"PrevFam:{previous_family}")
        if current_family and previous_family == current_family:
            essential.append("Repeat:changed-state")
        head = ". ".join(essential) + "."
    return head[:head_limit].rstrip() + tail


def visual_review_context(
    visual_story: Mapping[str, Any],
    beat_id: str,
    fallback_intent: str,
    *,
    previous_observation: str = "",
) -> str:
    """Keep the real action/proof intact inside the existing cloud review call.

    The legacy 300-character synopsis remains available for Engine compatibility.
    Canonical evidence has its own prompt and can preserve one bounded full beat.
    Retrieval queries never replace the immutable editorial meaning.
    """
    beats = [b for b in visual_story.get("beats", []) if isinstance(b, Mapping)]
    index = next((i for i, b in enumerate(beats) if str(b.get("id") or "") == beat_id), None)
    if index is None:
        return str(fallback_intent or "").strip()[:300]
    beat = beats[index]
    optional_support = [
        _context_fragment(cue, "", 80)
        for cue in list(beat.get("semantic_must_have") or [])[1:]
        if not _is_face_dependent_semantic_cue(cue)
    ][:3]
    core = [
        "Visual proof contract v2",
        f"Role:{_context_fragment(beat.get('role'), 'body', 12)}",
        f"Current: {_context_fragment(beat.get('shot_intent') or fallback_intent, 'visible action', 220)}",
        f"Meaning:{_context_fragment(beat.get('meaning_target') or beat.get('viewer_intent'), 'specific meaning', 180)}",
        # Only the first cue is the proof gate. Providers list up to four cues, often
        # alternative ways to show the idea; a single stock clip cannot hold all of
        # them, and judging "all of them" blocked an otherwise correct clip (run 109).
        "Must show:" + ", ".join(
            _context_fragment(cue, "", 110)
            for cue in list(beat.get("semantic_must_have") or [])[:1]
        ),
        *(
            ["Optional support (NOT required for PROOF: matched): " + ", ".join(optional_support)]
            if optional_support
            else []
        ),
        "Avoid:" + ", ".join(
            _context_fragment(cue, "", 65)
            for cue in list(beat.get("semantic_should_avoid") or [])[:4]
        ),
    ]
    if previous_observation:
        # This comes from the SAME already-completed Vision verdict, not the Plan's
        # guessed family. Replacements must not resurrect an adjacent old family.
        core.append("Previous accepted observation: " + _context_fragment(previous_observation, "", 190))
    elif index > 0:
        core.append("Previous: " + _context_fragment(beats[index - 1].get("shot_intent"), "", 100))
    else:
        core.append("Previous: story opening")
    if index + 1 < len(beats):
        core.append("Next: " + _context_fragment(beats[index + 1].get("shot_intent"), "", 100))
    else:
        core.append("Next: story arrival")
    core.append("Same hook-to-payoff arc: judge continuity")
    core.append("Judge specific meaning before mood; repeated actual family requires visible state change.")
    # Limits above keep every cue before optional continuity text, with room for
    # the AI image-only policy appended by the caller.
    return ". ".join(core)[:1600]
