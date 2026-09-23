from __future__ import annotations

import re
from typing import Any, Mapping

SHORT_SECTION_COUNT = 3
SHORT_WIDTH = 1080
SHORT_HEIGHT = 1920
# Legacy Moment/Short baseline used target=15s and a 7s floor. Clean V2 keeps
# that compact pacing but honors the channel's explicit absolute ceiling of 30s.
SHORT_TARGET_SECONDS = 15.0
SHORT_MIN_SECONDS = 7.0
SHORT_MAX_SECONDS = 30.0
SHORT_HOOK_MAX_WORDS = 12

TEMPLATE_ORDER = (
    "why_reframe",
    "inner_dialogue",
    "micro_story",
    "quote_reflection",
)

TEMPLATE_SIGNALS: dict[str, tuple[tuple[str, int], ...]] = {
    "why_reframe": (
        ("لماذا", 4),
        ("السبب", 3),
        ("المشكلة ليست", 5),
        ("الحقيقة", 3),
        ("في الحقيقة", 4),
        ("في الواقع", 3),
        ("ليس", 2),
        ("لكن", 1),
        ("بل", 2),
        ("تظن", 3),
        ("تعتقد", 3),
        ("خطأ", 3),
        ("وهم", 3),
        ("خرافة", 4),
        ("بدل", 2),
    ),
    "inner_dialogue": (
        ("قلت لنفسي", 6),
        ("أقول لنفسي", 6),
        ("سألت نفسي", 5),
        ("أسأل نفسي", 5),
        ("بيني وبين نفسي", 6),
        ("صوت داخلي", 5),
        ("في داخلي", 4),
        ("لا أستطيع", 5),
        ("لن أستطيع", 5),
        ("أخاف", 3),
        ("الخوف", 3),
        ("قلق", 3),
        ("متردد", 3),
        ("التردد", 3),
        ("الدافع", 3),
        ("فقد الدافع", 5),
        ("الثقة", 2),
        ("أشعر", 2),
        ("ماذا لو", 4),
    ),
    "micro_story": (
        ("قصة", 5),
        ("ذات يوم", 6),
        ("في يوم", 4),
        ("مرة", 3),
        ("عندما", 3),
        ("حين", 2),
        ("حدث", 4),
        ("بدأت", 3),
        ("قررت", 3),
        ("مررت", 3),
        ("تجربة", 3),
        ("في تلك اللحظة", 5),
        ("بعد ذلك", 3),
        ("ثم", 1),
    ),
    "quote_reflection": (
        ("اقتباس", 7),
        ("مقولة", 7),
        ("هذه العبارة", 6),
        ("تلك العبارة", 6),
        ("عبارة", 4),
        ("كما قال", 5),
        ("قال لي", 4),
        ("قالت لي", 4),
    ),
}

TEMPLATE_COMPENSATION = {
    "why_reframe": {
        "beat_shape": ["hook_misbelief", "contrast", "reframe_payoff"],
        "visual_rhythm": "contrast_reframe",
    },
    "inner_dialogue": {
        "beat_shape": ["inner_voice_hook", "friction_turn", "payoff_action"],
        "visual_rhythm": "intimate_pressure_release",
    },
    "micro_story": {
        "beat_shape": ["scene_hook", "event_turn", "meaning_payoff"],
        "visual_rhythm": "micro_narrative_progression",
    },
    "quote_reflection": {
        "beat_shape": ["quote_hook", "reflection", "payoff"],
        "visual_rhythm": "held_frame_then_release",
    },
}

TEMPLATE_VISUAL_QUERY_DIRECTIVES = {
    "why_reframe": (
        "For every visual_query_en, make the three sections form a visible contrast arc: section 1 should "
        "show the old/common mistaken framing, section 2 should show the visual turn or contrast, and "
        "section 3 should show the clearer/new framing in an observable stock-footage scene. Avoid generic "
        "productivity imagery that does not carry that contrast."
    ),
    "inner_dialogue": (
        "For every visual_query_en, use an intimate quiet human moment that visually supports internal "
        "dialogue: a person alone, thoughtful, reflective, pausing, sitting quietly, or moving through a "
        "calm environment. Prefer concrete search language such as alone, thoughtful, reflective, quiet "
        "moment, contemplative. Avoid generic desks, calendars, or unrelated symbolic footage."
    ),
    "micro_story": (
        "Make the three visual_query_en values a simple sequential micro-story about one concrete situation: "
        "section 1 establishes the starting situation, section 2 shows the small development/turn, and "
        "section 3 shows the visual outcome. Each query must describe a realistic observable stock-footage "
        "moment from that same miniature situation, not generic or purely symbolic footage."
    ),
    "quote_reflection": (
        "For every visual_query_en, use calm reflective footage that supports one central approved quotation "
        "or idea without competing detail: quiet setting, slow/simple action, restrained composition, and "
        "minimal visual distraction. Avoid busy motion, multiple simultaneous actions, or unrelated imagery."
    ),
}

INNER_DIALOGUE_VOICE_RULES = (
    "The narration must sound natural when spoken aloud in Modern Standard Arabic: short, concrete, "
    "and conversational rather than essay-like or analytical.",
    "Required progression: felt moment -> brief inner thought -> natural realization/turn -> one earned action.",
    'BAD: "قلت لنفسي: السبب الحقيقي ليس الإرهاق، بل أنك لم تحدد ما تريد."',
    'GOOD: "مرّ اليوم ولم أبدأ. القائمة بدت أكبر مني. ربما أحتاج بداية أصغر."',
    'Avoid formulaic self-help language such as "السبب الحقيقي", repeated "قلت لنفسي", and the '
    '"ليس X بل Y" construction unless the approved source itself requires that exact contrast.',
    "Do not explain the lesson to the viewer. Prefer one small observable detail or thought that lets "
    "the realization emerge naturally.",
    'Do not address the viewer with "افعل" / "ابدأ" / "عليك" except in the final line only, where '
    "at most one single-action imperative is allowed by the Short contract.",
    "The turn must sound discovered inside the moment, not preached by an external narrator.",
)

TEMPLATE_WRITING_DIRECTIVES = {
    "why_reframe": (
        "Short type is why_reframe. Open on one specific mistaken assumption, contrast it with the "
        "useful truth, reframe it, then land one concrete payoff/action. Keep the Arabic natural and "
        "specific; do not add generic motivation."
    ),
    "inner_dialogue": (
        "Short type is inner_dialogue. Open with an immediate internal-tension line, show the friction, "
        "turn the perspective, then land one practical payoff/action. Keep it intimate but not melodramatic "
        "and never fabricate autobiography. " + " ".join(INNER_DIALOGUE_VOICE_RULES)
    ),
    "micro_story": (
        "Short type is micro_story. Enter a tiny concrete scene immediately, show one event/turn, then land "
        "the meaning/payoff. Do not invent personal facts; use a generic human scenario unless the approved "
        "brief itself supplies a real event."
    ),
    "quote_reflection": (
        "Short type is quote_reflection. Use only an actual quotation explicitly present in the approved "
        "topic as the opening hook; never invent, alter, or attribute a quote. Follow with a brief reflection "
        "and a concrete payoff."
    ),
}


class ShortFormatError(RuntimeError):
    pass


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _semantic_key(value: object) -> str:
    text = _clean(value).casefold()
    text = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", text)
    text = text.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"}))
    return " ".join(re.sub(r"[^\w\u0600-\u06ff]+", " ", text).split())


def _signal_score(text: object, signals: tuple[tuple[str, int], ...]) -> int:
    normalized = f" {_semantic_key(text)} "
    return sum(
        weight
        for phrase, weight in signals
        if f" {_semantic_key(phrase)} " in normalized
    )


def _paired_quote(text: object) -> bool:
    raw = _clean(text)
    return any(
        opening in raw and closing in raw
        for opening, closing in (("«", "»"), ("“", "”"))
    ) or raw.count('"') >= 2


def _research_text(brief: Mapping[str, Any]) -> str:
    values: list[str] = []
    pack = brief.get("research_pack")
    if isinstance(pack, list):
        for row in pack:
            if not isinstance(row, Mapping):
                continue
            values.append(_clean(row.get("source_title")))
            values.append(_clean(row.get("claim_scope")))
    return " ".join(value for value in values if value)


def _emotional_goal_text(brief: Mapping[str, Any]) -> str:
    return " ".join(
        value
        for value in (
            _clean(brief.get("emotional_goal")),
            _clean(brief.get("emotional_arc")),
            _clean(brief.get("editorial_intent")),
        )
        if value
    )


def select_short_template(brief: Mapping[str, Any]) -> dict[str, Any]:
    """Choose one of the four frozen Short templates without another AI call.

    The approved topic is authoritative and receives the strongest weight. Approved
    evidence and the declared emotional/editorial goal only refine ties and weak
    signals; generated plan/script text is never allowed to steer its own template.
    """
    topic = _clean(brief.get("approved_topic"))
    if not topic:
        raise ShortFormatError("short_template_requires_approved_topic")

    evidence = _research_text(brief)
    emotional_goal = _emotional_goal_text(brief)
    scores: dict[str, int] = {}
    for template in TEMPLATE_ORDER:
        signals = TEMPLATE_SIGNALS[template]
        scores[template] = (
            3 * _signal_score(topic, signals)
            + _signal_score(evidence, signals)
            + 2 * _signal_score(emotional_goal, signals)
        )

    topic_key = f" {_semantic_key(topic)} "
    if " لماذا " in topic_key:
        scores["why_reframe"] += 4
    if " كيف " in topic_key:
        scores["inner_dialogue"] += 2

    quote_evidence = _paired_quote(topic)
    if not quote_evidence:
        scores["quote_reflection"] = -100

    if max(scores.values()) <= 0:
        pillar = _clean(brief.get("pillar")).lower()
        fallback = {
            "understand": "why_reframe",
            "rise": "inner_dialogue",
            "see": "micro_story",
        }.get(pillar, "why_reframe")
        scores[fallback] = 1

    best = max(scores.values())
    template = next(item for item in TEMPLATE_ORDER if scores[item] == best)
    if template == "quote_reflection" and not quote_evidence:
        raise ShortFormatError("quote_reflection_requires_explicit_quote")

    return {
        "schema_version": 1,
        "template": template,
        "scores": scores,
        "selection_basis": "approved_topic_plus_evidence_plus_emotional_goal",
        "quote_evidence": quote_evidence,
        "beat_shape": TEMPLATE_COMPENSATION[template]["beat_shape"],
        "visual_rhythm": TEMPLATE_COMPENSATION[template]["visual_rhythm"],
        "writing_directive": TEMPLATE_WRITING_DIRECTIVES[template],
        "visual_query_directive": TEMPLATE_VISUAL_QUERY_DIRECTIVES[template],
        "extra_ai_calls": 0,
    }


def short_prompt_context(brief: Mapping[str, Any]) -> str:
    selection = select_short_template(brief)
    beats = " -> ".join(selection["beat_shape"])
    return (
        "SHORT_FORMAT_CONTRACT:\n"
        f"- selected_template={selection['template']}\n"
        f"- beat_shape={beats}\n"
        f"- target_duration_seconds={SHORT_TARGET_SECONDS:g}; hard_range="
        f"{SHORT_MIN_SECONDS:g}-{SHORT_MAX_SECONDS:g}\n"
        f"- exact_sections={SHORT_SECTION_COUNT}; frame={SHORT_WIDTH}x{SHORT_HEIGHT}\n"
        f"- s1: the first spoken sentence is the truthful hook and must be at most {SHORT_HOOK_MAX_WORDS} Arabic words; no greeting. "
        "It must create immediate viewer tension by naming one concrete felt friction, contradiction, or unresolved consequence. "
        "Do not open with an abstract definition, generic \"sometimes\" setup, or a formulaic X-is-not-Y-but-Z explanation.\n"
        "- s2: advance the hook with the selected template's specific cause/turn; add new information instead of paraphrasing s1 or switching to generic motivation.\n"
        "- s3: resolve the SAME tension/question opened by s1-s2, then give exactly ONE practical action in one clear imperative sentence. "
        "That action sentence MUST begin with a direct Arabic imperative verb, not a descriptive suggestion, and must not append a second action with ثم/و. "
        "Good examples: \"ابدأ بـ...\", \"جرّب أن...\", \"افعل...\", \"اختر...\", \"اكتب...\". "
        "Bad examples: \"اكتب... ثم اخرج...\", \"يمكنك أن...\", \"من الأفضل أن...\", or a general description with no command.\n"
        "- No channel identity opener, dialogue labels, social CTA, or quotation unless the selected "
        "quote_reflection template has explicit approved quote evidence.\n"
        f"- {selection['writing_directive']}\n"
        "- SPOKEN_NATURALNESS_LITE: write for the ear, not the page. Keep sentences short and concrete; "
        "avoid abstract diagnosis, polished essay transitions, generic motivational slogans, and repeated rhetorical formulas. "
        "If a sentence sounds like a coach explaining a lesson rather than a person noticing a real moment, rewrite it more simply.\n"
        "- VISUAL_QUERY_DIRECTION: "
        f"{TEMPLATE_VISUAL_QUERY_DIRECTIVES[selection['template']]} "
        "Across s1/s2/s3, use visibly different dominant actions or states so the picture itself progresses. "
        "For s1 prefer an immediately readable active friction/decision over a passive generic desk shot. "
        "For s3 depict the single payoff action itself or its immediate visible result; never repeat the same writing/desk action used earlier."
    )


def validate_short_duration(seconds: float, *, phase: str) -> float:
    value = float(seconds)
    if not SHORT_MIN_SECONDS <= value <= SHORT_MAX_SECONDS:
        raise ShortFormatError(
            "short_duration_out_of_range "
            f"phase={phase} seconds={value:.3f} "
            f"allowed={SHORT_MIN_SECONDS:g}-{SHORT_MAX_SECONDS:g}"
        )
    return value


def validate_short_dimensions(width: int, height: int) -> tuple[int, int]:
    actual = (int(width), int(height))
    expected = (SHORT_WIDTH, SHORT_HEIGHT)
    if actual != expected:
        raise ShortFormatError(
            f"short_frame_mismatch actual={actual[0]}x{actual[1]} "
            f"expected={expected[0]}x{expected[1]}"
        )
    return actual


_HOOK_SENTENCE_END_RE = re.compile(r"[.!؟!]")
_DIALOGUE_LABEL_RE = re.compile(r"(?m)^\s*[AB]:\s*\S")
_SOCIAL_CTA_RE = re.compile(
    r"(?:اشترك|اشترِك|تابعنا|تابعني|شارك(?:ها|ه|ني)|"
    r"شارك\s+(?:هذه|هذا|الفكرة|المقطع|الفيديو|الحلقة)|"
    r"اكتب.{0,24}(?:التعليقات|تعليق)|علّق|علق|"
    r"اضغط.{0,16}(?:إعجاب|اعجاب|لايك)|ضع.{0,16}(?:إعجاب|اعجاب|لايك))",
    re.I,
)
_GREETING_PREFIXES = (
    "اهلا",
    "اهلا وسهلا",
    "مرحبا",
    "السلام عليكم",
    "صباح الخير",
    "مساء الخير",
)

_PRACTICAL_ACTION_MARKERS = (
    "اختر",
    "افعل",
    "ابدأ",
    "اكتب",
    "حدد",
    "حدّد",
    "ضع",
    "حوّل",
    "حول",
    "اربط",
    "جرّب",
    "جرب",
    "خذ",
    "اترك",
    "اجعل",
    "خصص",
    "خصّص",
    "افتح",
    "اغلق",
    "أغلق",
    "نفذ",
    "نفّذ",
    "اخرج",
    "امش",
    "تحرك",
    "تحرّك",
    "راقب",
    "اقرأ",
    "اقرا",
    "توقف",
    "توقّف",
    "قم",
)


def _first_sentence(text: object) -> str:
    compact = _clean(text)
    if not compact:
        return ""
    match = _HOOK_SENTENCE_END_RE.search(compact)
    return compact[: match.end()].strip() if match else compact


def _word_count(text: object) -> int:
    return len([word for word in _clean(text).split() if word])


_SAFE_HOOK_TRIM_MAX_OVERRUN = 2
_SAFE_HOOK_TRIM_MIN_WORDS = 10
_SAFE_HOOK_BOUNDARY_CONJUNCTIONS = {"لكن", "ولكن", "و"}
_SAFE_HOOK_INCOMPLETE_ENDINGS = {
    "في", "من", "إلى", "الى", "على", "عن", "مع", "بلا", "بدون", "دون",
    "قبل", "بعد", "عند", "بين", "خلال", "لدى", "أن", "ان", "إن", "لأن", "لان",
    "حتى", "كي", "ثم", "أو", "او", "بل", "لكن", "و", "إذا", "اذا", "عندما", "حين",
}
_SAFE_HOOK_INCOMPLETE_KEYS = {
    _semantic_key(item) for item in _SAFE_HOOK_INCOMPLETE_ENDINGS
}


def _safe_short_hook_trim_candidate(hook: str) -> str | None:
    """Return a conservative local trim only for a 1-2 word hook overrun."""
    words = _clean(hook).split()
    overrun = len(words) - SHORT_HOOK_MAX_WORDS
    if overrun not in (1, 2):
        return None

    candidates: list[int] = []
    ceiling = min(SHORT_HOOK_MAX_WORDS, len(words))
    for index in range(ceiling):
        word = words[index]
        position = index + 1
        if position < _SAFE_HOOK_TRIM_MIN_WORDS:
            continue

        if re.search(r"[،,.؟!]$", word):
            candidates.append(position)
        normalized = re.sub(r"^[^\w\u0600-\u06ff]+|[^\w\u0600-\u06ff]+$", "", word)
        if normalized in _SAFE_HOOK_BOUNDARY_CONJUNCTIONS and index >= _SAFE_HOOK_TRIM_MIN_WORDS:
            candidates.append(index)

    for cut in reversed(candidates):
        if cut < _SAFE_HOOK_TRIM_MIN_WORDS:
            continue
        kept = words[:cut]
        if not kept:
            continue
        last = re.sub(r"[^\w\u0600-\u06ff]+$", "", kept[-1])
        if not last or _semantic_key(last) in _SAFE_HOOK_INCOMPLETE_KEYS:
            continue

        text = " ".join(kept).strip()
        text = re.sub(r"[،,؛;:.!?؟!]+$", "", text).strip()
        if not text or _word_count(text) > SHORT_HOOK_MAX_WORDS:
            continue
        return text + "."
    return None


def apply_safe_short_hook_trim(script: dict[str, Any]) -> bool:
    """Trim only a tiny overrun at a proven natural boundary; otherwise do nothing."""
    sections = script.get("sections")
    if not isinstance(sections, list) or not sections or not isinstance(sections[0], dict):
        return False

    narration = _clean(sections[0].get("narration"))
    hook = _first_sentence(narration)
    if not hook or _word_count(hook) <= SHORT_HOOK_MAX_WORDS:
        return False

    trimmed = _safe_short_hook_trim_candidate(hook)
    if trimmed is None:
        return False

    remainder = narration[len(hook):].lstrip()
    sections[0]["narration"] = f"{trimmed} {remainder}".strip()
    return True


def _practical_action_marker_count(text: object) -> int:
    compact = _clean(text)
    if not compact:
        return 0
    unique_markers = dict.fromkeys(_PRACTICAL_ACTION_MARKERS)
    return sum(
        len(re.findall(rf"(?<!\w){re.escape(marker)}(?!\w)", compact, flags=re.I))
        for marker in unique_markers
    )


def validate_short_hook_contract(script: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the provider-owned first spoken sentence before script acceptance."""
    sections = script.get("sections")
    if not isinstance(sections, list) or not sections or not isinstance(sections[0], Mapping):
        raise ShortFormatError("short_hook_requires_first_section")

    first_narration = _clean(sections[0].get("narration"))
    hook = _first_sentence(first_narration)
    if not hook:
        raise ShortFormatError("short_hook_missing")
    hook_words = _word_count(hook)
    if hook_words > SHORT_HOOK_MAX_WORDS:
        raise ShortFormatError(
            f"short_hook_too_long words={hook_words} maximum={SHORT_HOOK_MAX_WORDS}"
        )

    hook_key = _semantic_key(hook)
    if any(
        hook_key == prefix or hook_key.startswith(prefix + " ")
        for prefix in _GREETING_PREFIXES
    ):
        raise ShortFormatError("short_hook_must_not_start_with_greeting")

    return {"hook": hook, "hook_words": hook_words}


def validate_short_script(script: Mapping[str, Any]) -> dict[str, Any]:
    sections = script.get("sections")
    if not isinstance(sections, list) or len(sections) != SHORT_SECTION_COUNT:
        raise ShortFormatError(
            f"short_script_requires_exactly_{SHORT_SECTION_COUNT}_sections"
        )
    if any(not isinstance(item, Mapping) for item in sections):
        raise ShortFormatError("short_script_section_invalid")

    hook_report = validate_short_hook_contract(script)
    hook = str(hook_report["hook"])
    hook_words = int(hook_report["hook_words"])

    transcript = "\n".join(_clean(item.get("narration")) for item in sections)
    if _DIALOGUE_LABEL_RE.search(transcript):
        raise ShortFormatError("short_single_voice_contract_forbids_dialogue_labels")
    if _SOCIAL_CTA_RE.search(transcript):
        raise ShortFormatError("short_zero_social_cta_contract_violated")

    s3 = _clean(sections[2].get("narration"))
    action_sentences = [
        sentence
        for sentence in re.split(r"(?<=[.!؟!])\s+", s3)
        if any(
            _semantic_key(marker) in _semantic_key(sentence)
            for marker in _PRACTICAL_ACTION_MARKERS
        )
    ]
    if len(action_sentences) != 1:
        raise ShortFormatError(
            "short_s3_requires_exactly_one_practical_action "
            f"action_sentences={len(action_sentences)}"
        )
    action_marker_count = _practical_action_marker_count(action_sentences[0])
    if action_marker_count != 1:
        raise ShortFormatError(
            "short_s3_requires_one_action_only "
            f"imperative_markers={action_marker_count}"
        )

    return {
        "hook": hook,
        "hook_words": hook_words,
        "single_voice": True,
        "social_cta": False,
        "practical_action_sentences": 1,
        "practical_action_markers": action_marker_count,
    }


_VISUAL_QUERY_TERMS = {
    "why_reframe": {
        "old": frozenset({"confused", "cluttered", "overwhelmed", "stuck", "wrong", "messy", "frustrated", "chaotic"}),
        "turn": frozenset({"pause", "reconsidering", "changing", "adjusting", "rewriting", "switching", "turning", "reframing", "contrast"}),
        "new": frozenset({"clear", "organized", "simple", "focused", "calm", "intentional", "practical", "steady"}),
    },
    "inner_dialogue": frozenset({"alone", "thoughtful", "reflective", "quiet", "contemplative", "thinking", "solitary", "pensive"}),
    "quote_reflection": frozenset({"quiet", "calm", "reflective", "still", "slow", "peaceful", "contemplative", "minimal"}),
}

_MICRO_STORY_ACTION_TERMS = frozenset({
    "walking", "entering", "opening", "closing", "writing", "reading", "placing",
    "picking", "starting", "stopping", "sitting", "standing", "leaving", "returning",
    "checking", "packing", "unpacking", "preparing", "working", "waiting", "turning",
    "moving", "reaching", "holding", "setting", "putting", "taking",
})

_VISUAL_ACTION_FAMILIES = {
    "writing_desk": frozenset({"write", "writing", "rewriting", "notebook", "journal", "paper", "desk", "typing"}),
    "walking": frozenset({"walk", "walking", "leaving", "moving", "steps", "path"}),
    "phone": frozenset({"phone", "scrolling", "screen", "checking"}),
    "reading": frozenset({"read", "reading", "book"}),
    "organizing": frozenset({"organizing", "sorting", "placing", "packing", "arranging"}),
}


def _query_words(value: object) -> set[str]:
    return set(re.findall(r"[a-z]+", _clean(value).casefold()))


def _query_action_families(words: set[str]) -> set[str]:
    return {
        family
        for family, terms in _VISUAL_ACTION_FAMILIES.items()
        if words & terms
    }


def validate_short_visual_queries(
    plan: Mapping[str, Any],
    brief: Mapping[str, Any],
) -> dict[str, Any]:
    """Fail closed when Short planning ignored its selected template's visual language.

    This checks only the generated stock-search phrases. It does not alter acquisition,
    Canonical Evidence, candidate scoring, or Visual QA.
    """
    selection = select_short_template(brief)
    template = str(selection["template"])
    sections = plan.get("sections")
    if not isinstance(sections, list) or len(sections) != SHORT_SECTION_COUNT:
        raise ShortFormatError("short_visual_query_contract_requires_three_sections")
    queries = [_clean(item.get("visual_query_en")) for item in sections if isinstance(item, Mapping)]
    if len(queries) != SHORT_SECTION_COUNT or any(not query for query in queries):
        raise ShortFormatError("short_visual_query_missing")
    words = [_query_words(query) for query in queries]

    if template == "inner_dialogue":
        allowed = _VISUAL_QUERY_TERMS[template]
        if any(not (item & allowed) for item in words):
            raise ShortFormatError("short_visual_query_inner_dialogue_not_reflective")
        action_families = [_query_action_families(item) for item in words]
        if action_families[0] and action_families[2] and action_families[0] & action_families[2]:
            raise ShortFormatError("short_visual_query_inner_dialogue_payoff_repeats_opening_action")
        if action_families[1] and action_families[2] and action_families[1] & action_families[2]:
            raise ShortFormatError("short_visual_query_inner_dialogue_payoff_repeats_middle_action")
    elif template == "quote_reflection":
        allowed = _VISUAL_QUERY_TERMS[template]
        if any(not (item & allowed) for item in words):
            raise ShortFormatError("short_visual_query_quote_reflection_not_calm")
    elif template == "why_reframe":
        terms = _VISUAL_QUERY_TERMS[template]
        if not (words[0] & terms["old"]):
            raise ShortFormatError("short_visual_query_why_reframe_old_frame_missing")
        if not (words[1] & terms["turn"]):
            raise ShortFormatError("short_visual_query_why_reframe_turn_missing")
        if not (words[2] & terms["new"]):
            raise ShortFormatError("short_visual_query_why_reframe_new_frame_missing")
        action_families = [_query_action_families(item) for item in words]
        if action_families[0] and action_families[2] and action_families[0] & action_families[2]:
            raise ShortFormatError("short_visual_query_why_reframe_payoff_repeats_opening_action")
    elif template == "micro_story":
        if any(not (item & _MICRO_STORY_ACTION_TERMS) for item in words):
            raise ShortFormatError("short_visual_query_micro_story_action_missing")
        generic = {
            "person", "man", "woman", "young", "adult", "alone", "indoors", "outdoors",
            "close", "wide", "shot", "camera", "video", "footage",
        }
        content_sets = [item - generic - _MICRO_STORY_ACTION_TERMS for item in words]
        shared = set.intersection(*content_sets) if content_sets else set()
        if not shared:
            raise ShortFormatError("short_visual_query_micro_story_scene_continuity_missing")

    return {
        "template": template,
        "queries": queries,
        "action_families": [sorted(_query_action_families(item)) for item in words],
        "status": "pass",
    }

def short_contract_report(brief: Mapping[str, Any]) -> dict[str, Any]:
    selection = select_short_template(brief)
    return {
        **selection,
        "format": "short",
        "section_count": SHORT_SECTION_COUNT,
        "frame": {"width": SHORT_WIDTH, "height": SHORT_HEIGHT},
        "duration": {
            "target_seconds": SHORT_TARGET_SECONDS,
            "minimum_seconds": SHORT_MIN_SECONDS,
            "maximum_seconds": SHORT_MAX_SECONDS,
        },
        "hook": {
            "first_spoken_sentence": True,
            "maximum_words": 12,
            "greeting_forbidden": True,
        },
        "voice": {
            "single_narrator": True,
            "primary_voice": "Charon",
            "dialogue_labels_forbidden": True,
            "orus_two_voice_support": "deferred_short_v2",
        },
        "social_cta": "forbidden",
        "narrative_identity": "not_applicable",
        "opening_director": "not_applicable",
    }
