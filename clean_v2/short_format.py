from __future__ import annotations

import re
from typing import Any, Mapping

SHORT_SECTION_COUNT = 3
SHORT_WIDTH = 1080
SHORT_HEIGHT = 1920
# Timeline First: duration is editorially unconstrained. The measured mastered voice
# owns runtime; this distant ceiling is operational runaway protection only.
SHORT_DURATION_SAFETY_MAX_SECONDS = 120.0
SHORT_HOOK_MAX_WORDS = 18
# Emergency acceptance headroom only. Production normalization always attempts a
# conservative local trim back to SHORT_HOOK_MAX_WORDS first. Keeping this
# separate prevents providers from treating 20 words as the writing target.
SHORT_HOOK_RESCUE_MAX_WORDS = 20
# A longer provider hook may still contain two clean thoughts. We may split it
# locally only at a proven natural boundary, preserving every word, instead of
# spending another provider call. Anything beyond this stays fail-closed.
SHORT_HOOK_SAFE_SPLIT_MAX_WORDS = 32
SHORT_HOOK_PREFERRED_MIN_WORDS = 8
SHORT_HOOK_PREFERRED_MAX_WORDS = 16

# Deterministic local hook-shape gate. This detects structure, not emotional
# intensity: calm curiosity is valid, clickbait is not. A Short must enter
# through one of three explicit shapes instead of spending s1 on generic setup.
_SHORT_HOOK_GENERIC_OPENERS = (
    "احيانا",
    "في حياتنا",
    "في الحياه",
    "في بعض الاحيان",
    "كلنا",
    "من الطبيعي",
    "من المهم",
    "هناك اوقات",
    "مع مرور الوقت",
    "اليوم سنتحدث",
    "في هذا الفيديو",
)
_SHORT_HOOK_QUESTION_OPENERS = (
    "هل",
    "لماذا",
    "كيف",
    "ماذا",
    "متي",
    "اين",
    "من",
    "ما الذي",
    "ماذا لو",
)
_SHORT_HOOK_CONSEQUENCE_TERMS = frozenset({
    "تفقد", "تخسر", "ينهار", "تنهار", "يفشل", "تفشل", "يتحول", "تتحول",
    "يتراكم", "تتراكم", "يزداد", "تزداد", "يتضاعف", "تتضاعف", "يستنزف",
    "تستنزف", "ينتهي", "تنتهي", "يختفي", "تختفي", "يتوقف", "تتوقف", "يمنعك", "تمنعك",
    "يجعلك", "تجعلك", "يسرق", "تسرق", "يكلف", "تكلف", "تتعطل", "تعلق",
})

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
        "For every visual_query_en, make the three sections form a visible contrast arc. "
        "For s1 use an immediately readable contradiction, mistake, interruption, or consequence that feels visually unresolved "
        "and strong enough to stop the scroll without melodrama. "
        "For s2 show the visual turn or contrast. For s3 show the clearer/new framing as a decisive observable payoff. "
        "Avoid generic productivity imagery that does not carry that contrast."
    ),
    "inner_dialogue": (
        "Keep one coherent inner-dialogue arc, but do NOT make the hook visually calm. "
        "For s1 show immediate active friction or pressure in an observable no-face moment: hesitation, "
        "a hand stopping mid-action, a rushed/tense detail, an unfinished task, or another readable visual conflict. "
        "For s2 let the image become more reflective as the thought turns. "
        "For s3 show a decisive single payoff action or its immediate visible result. "
        "Prefer concrete no-face search language and avoid generic passive desks, calendars, or unrelated symbolism."
    ),
    "micro_story": (
        "Make the three visual_query_en values a simple sequential micro-story about one concrete situation. "
        "For s1 begin inside an observable action or event already in motion; do not spend the hook on a quiet establishing shot. "
        "For s2 show the development/turn, and for s3 show a clear visible outcome/payoff. "
        "Each query must describe a realistic observable stock-footage moment from that same miniature situation, "
        "not generic or purely symbolic footage."
    ),
    "quote_reflection": (
        "Keep the reflective identity, but make s1 visually arresting through composition rather than frantic motion: "
        "a striking close detail, strong light/shadow contrast, meaningful object, or immediate visual tension that can hold a quote. "
        "For s2 return to calm reflective footage with minimal distraction. For s3 show a clean release or concrete payoff image. "
        "Avoid busy motion, multiple simultaneous actions, or unrelated imagery."
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

# Universal (all four templates): direct answer to "feels AI-written, padded,
# and the payoff isn't earned" feedback on real production output. Mirrors
# EDITORIAL_VOICE_ADVISORY in tone_audit.py's shared audit prompt exactly, so
# the writer is asked to do the same thing the (currently advisory-only, non-
# blocking) audit separately observes - closing the loop from "measured
# after the fact" to "the writer actually tries", without yet gating on it.
HUMAN_VOICE_NO_FILLER = (
    "Every sentence must add new information, feeling, or forward motion - never restate a point "
    "already made in different words, and never use a hollow transition that carries no content "
    '(e.g. "لكن الحقيقة أن", "في الواقع") unless it introduces something genuinely new. The closing '
    "payoff must depend on a specific concrete detail already established earlier in this same "
    "narration - never a generic statement that could just as easily close a different topic."
)

# Selective (only templates whose own shape is a scene/inner-voice/narrative
# turn): forcing a scene-open on why_reframe or quote_reflection would fight
# their own explicitly argumentative/quotation-led design, so this is never
# applied there.
COLD_OPEN_AS_SCENE = (
    "The opening line must land inside a concrete moment, sensation, or action already under way - "
    "never a general statement, address, or instruction."
)

TEMPLATE_WRITING_DIRECTIVES = {
    "why_reframe": (
        "Short type is why_reframe. Open on one specific mistaken assumption, contrast it with the "
        "useful truth, reframe it, then land one concrete payoff/action. Keep the Arabic natural and "
        "specific; do not add generic motivation. " + HUMAN_VOICE_NO_FILLER
    ),
    "inner_dialogue": (
        "Short type is inner_dialogue. Open with an immediate internal-tension line, show the friction, "
        "turn the perspective, then land one practical payoff/action. Keep it intimate but not melodramatic "
        "and never fabricate autobiography. " + " ".join(INNER_DIALOGUE_VOICE_RULES) + " "
        + HUMAN_VOICE_NO_FILLER + " " + COLD_OPEN_AS_SCENE
    ),
    "micro_story": (
        "Short type is micro_story. Enter a tiny concrete scene immediately, show one event/turn, then land "
        "the meaning/payoff. Do not invent personal facts; use a generic human scenario unless the approved "
        "brief itself supplies a real event. " + HUMAN_VOICE_NO_FILLER + " " + COLD_OPEN_AS_SCENE
    ),
    "quote_reflection": (
        "Short type is quote_reflection. Use only an actual quotation explicitly present in the approved "
        "topic as the opening hook; never invent, alter, or attribute a quote. Follow with a brief reflection "
        "and a concrete payoff. " + HUMAN_VOICE_NO_FILLER
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


def _starts_with_semantic_phrase(value: str, phrases: tuple[str, ...]) -> bool:
    return any(value == phrase or value.startswith(phrase + " ") for phrase in phrases)


def validate_short_hook_immediate_tension(hook: object) -> str:
    """Require an immediate concrete tension shape without rewarding clickbait.

    Accepted deterministic shapes:
    - direct question: explicit Arabic question opener + question mark;
    - contrast/paradox: an explicit turn such as لكن/رغم/مع أن/ليس...بل;
    - result/consequence-first: a concrete failure/loss/escalation verb appears early.

    Generic/calm setup is rejected only when none of those concrete shapes exists.
    """
    raw = _clean(hook)
    key = _semantic_key(raw)
    if not raw or not key:
        raise ShortFormatError("short_hook_immediate_tension_missing")

    if _starts_with_semantic_phrase(key, _SHORT_HOOK_GENERIC_OPENERS):
        raise ShortFormatError("short_hook_generic_calm_opening")

    question = (
        ("؟" in raw or "?" in raw)
        and _starts_with_semantic_phrase(key, _SHORT_HOOK_QUESTION_OPENERS)
    )

    tokens = key.split()
    contrast = (
        ("ليس" in tokens and "بل" in tokens)
        or any(token.startswith("لكن") for token in tokens[1:])
        or "رغم" in tokens
        or ("مع" in tokens and "ان" in tokens)
        or ("علي" in tokens and "الرغم" in tokens)
        or "المفارقه" in tokens
        or "الغريب" in tokens
        or bool(re.search(r"\bلم\s+\S+", key))
        or bool(re.search(r"\bلا\s+تكون\s+المشكله\b", key))
    )

    # Keep result-first evidence near the start so a generic setup cannot hide the
    # actual tension only in the final words.
    early_tokens = set(tokens[: max(6, min(10, len(tokens)))])
    consequence = bool(early_tokens & _SHORT_HOOK_CONSEQUENCE_TERMS)

    if question or contrast or consequence:
        return (
            "direct_question"
            if question
            else ("explicit_contrast" if contrast else "early_consequence")
        )

    raise ShortFormatError("short_hook_requires_immediate_concrete_tension")


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

    # The shared brief freezes the same exclusions for every call in this run.
    recent = tuple(
        str(value).strip()
        for value in (brief.get("_recent_templates") or ())
        if str(value or "").strip()
    )
    selection_basis = "approved_topic_plus_evidence_plus_emotional_goal"
    if recent:
        history_scores = dict(scores)
        for name in recent:
            if name in history_scores:
                history_scores[name] = -100
        # Preserve quote eligibility and fall back if every eligible shape is excluded.
        if max(history_scores.values()) > -100:
            scores = history_scores
            selection_basis += "_history_aware"

    best = max(scores.values())
    template = next(item for item in TEMPLATE_ORDER if scores[item] == best)
    if template == "quote_reflection" and not quote_evidence:
        raise ShortFormatError("quote_reflection_requires_explicit_quote")

    return {
        "schema_version": 1,
        "template": template,
        "scores": scores,
        "selection_basis": selection_basis,
        "recent_templates_excluded": list(recent),
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
        f"- duration_owner=measured_voice; editorial_target_duration=none; "
        f"operational_safety_max_seconds={SHORT_DURATION_SAFETY_MAX_SECONDS:g}\n"
        f"- exact_sections={SHORT_SECTION_COUNT}; frame={SHORT_WIDTH}x{SHORT_HEIGHT}\n"
        f"- s1: the first spoken sentence is the truthful hook: one complete, natural Arabic sentence, preferably "
        f"{SHORT_HOOK_PREFERRED_MIN_WORDS}-{SHORT_HOOK_PREFERRED_MAX_WORDS} words and never more than {SHORT_HOOK_MAX_WORDS}; no greeting. "
        "Choose the hook family that best fits the topic (paradox, direct scene, real question, result-first, unexpected observation, "
        "common-belief break, hidden cost, or cold open). It must create a real information gap without becoming clickbait. "
        "LOCAL HOOK SHAPE GATE: s1 must be either a direct question, an explicit contrast/paradox (for example لكن/رغم/ليس...بل), "
        "or an early concrete consequence/result. A generic descriptive setup by itself is rejected even when fluent. "
        "Do not sacrifice grammar or meaning just to make it shorter.\n"
        "- s2: advance the hook with the selected template's specific cause/turn; add new information instead of paraphrasing s1 or switching to generic motivation. Keep the pressure moving; do not drop into a long explanatory lull.\n"
        "- s3: resolve the SAME tension/question opened by s1-s2 with a concrete earned descriptive payoff; it must feel like a strong answer to the hook. Planning owns exactly ONE practical action in the top-level practical_action_ar field; Script MUST NOT invent, repeat, paraphrase, or replace that action because runtime appends the locked sentence after the descriptive payoff. "
        "For Planning, practical_action_ar MUST begin with a direct Arabic imperative verb, contain exactly ONE recognized imperative/action marker, and express exactly ONE practical action. Its first word MUST be one of these validator-recognized imperatives: اختر، افعل، ابدأ، اكتب، حدد، حدّد، ضع، حوّل، حول، اربط، جرّب، جرب، خذ، اترك، اجعل، خصص، خصّص، افتح، اغلق، أغلق، نفذ، نفّذ، اخرج، امش، تحرك، تحرّك، راقب، اقرأ، اقرا، توقف، توقّف، التزم، قم. It must not append a second action with ثم/و, an attached conjunction such as والتزم/واكتب, punctuation, or another clause. "
        "For Script, every authored s3 sentence must be purely descriptive, with ZERO command verbs and ZERO occurrences or derivatives of these action families: "
        "اختر، افعل، ابدأ، اكتب، حدد، حدّد، ضع، حوّل، حول، اربط، جرّب، جرب، خذ، اترك، اجعل، خصص، خصّص، افتح، اغلق، أغلق، نفذ، نفّذ، اخرج، امش، تحرك، تحرّك، راقب، اقرأ، اقرا، توقف، توقّف، التزم، قم. "
        "Planning self-check: practical_action_ar contains exactly one imperative marker. It must DIRECTLY operationalize this video's own hook_tension/payoff_answer and name the same topic-specific object, contrast, or behavior; if the same action could close an unrelated self-development Short, rewrite it. Script self-check: s3 contains zero imperative markers because the host adds the locked Planning action afterward.\n"
        "- No channel identity opener, dialogue labels, social CTA, or quotation unless the selected "
        "quote_reflection template has explicit approved quote evidence.\n"
        f"- {selection['writing_directive']}\n"
        "- SPOKEN_NATURALNESS_LITE: write for the ear, not the page, but never dumb the idea down. Use complete, grammatically sound "
        "sentences that carry enough context to be understood on first listen. Prefer concrete observations and natural sentence-length variation; "
        "avoid fragments, abstract diagnosis, polished essay transitions, generic motivational slogans, and repeated rhetorical formulas. "
        "Before returning JSON, silently reread EVERY authored Arabic sentence for demonstrative/noun agreement, pronoun/reference agreement, "
        "and sentence completeness. No section may open with a dependent fragment such as «مما ...» unless its grammatical antecedent is inside "
        "that same sentence. Fix any such surface-language error in place before returning. "
        "The whole Short should feel like one complete miniature idea, not a chain of motivational captions.\n"
        "- VISUAL_QUERY_DIRECTION: "
        f"{TEMPLATE_VISUAL_QUERY_DIRECTIVES[selection['template']]} "
        "Across s1/s2/s3, use visibly different dominant actions or states so the picture itself progresses. "
        "For s1 create the template-specific scroll-stop visual beat described above; it must read instantly and must not feel visually flat. "
        "LOCAL FIRST-SHOT GATE: visual_query_en for s1 must name an observable action/event AND a readable tension/consequence "
        "(for example stopping mid-action + unfinished task, rushing + deadline, ringing alarm + missed work). "
        "Quiet thinking, a generic desk, a static person, or a landscape establishing shot alone is rejected. "
        "For s3 depict the single payoff action itself or its immediate visible result with a clear sense of release/completion; never repeat the same writing/desk action used earlier."
    )


def validate_short_duration(seconds: float, *, phase: str) -> float:
    """Validate only the operational safety ceiling; never an editorial target."""
    value = float(seconds)
    if value <= 0 or value > SHORT_DURATION_SAFETY_MAX_SECONDS:
        raise ShortFormatError(
            "short_duration_operational_safety_violation "
            f"phase={phase} seconds={value:.3f} "
            f"safety_maximum={SHORT_DURATION_SAFETY_MAX_SECONDS:g}"
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
    "التزم",
    "قم",
)

_PRACTICAL_ACTION_OBJECT_SUFFIX_MARKERS = frozenset({
    "اختر",
    "افعل",
    "اكتب",
    "حدد",
    "حدّد",
    "ضع",
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
    "راقب",
    "اقرأ",
    "اقرا",
})
_PRACTICAL_ACTION_OBJECT_SUFFIXES = (
    "هما",
    "كما",
    "هم",
    "هن",
    "كم",
    "كن",
    "ها",
    "نا",
    "ني",
    "ه",
    "ك",
    "ي",
)


def _practical_action_pattern(marker: str) -> str:
    suffix = ""
    if marker in _PRACTICAL_ACTION_OBJECT_SUFFIX_MARKERS:
        suffix = "(?:" + "|".join(
            re.escape(item) for item in _PRACTICAL_ACTION_OBJECT_SUFFIXES
        ) + ")?"
    return rf"(?<!\w){re.escape(marker)}{suffix}(?!\w)"


def _conjoined_practical_action_pattern(marker: str) -> str:
    """Match a second imperative joined to the prior clause as one Arabic word.

    Arabic commonly attaches conjunctions to the following verb (e.g. والتزم,
    واكتب). The main marker regex intentionally requires a word boundary so it
    does not over-count arbitrary substrings; this narrow companion catches only
    a leading waw/fa immediately before a configured imperative.
    """
    suffix = ""
    if marker in _PRACTICAL_ACTION_OBJECT_SUFFIX_MARKERS:
        suffix = "(?:" + "|".join(
            re.escape(item) for item in _PRACTICAL_ACTION_OBJECT_SUFFIXES
        ) + ")?"
    return rf"(?<!\w)[وف]{re.escape(marker)}{suffix}(?!\w)"


def _practical_action_base(word: str) -> str | None:
    normalized = _semantic_key(word)
    allowed = {_semantic_key(marker): marker for marker in _PRACTICAL_ACTION_MARKERS}
    if normalized in allowed:
        return allowed[normalized]
    for marker in _PRACTICAL_ACTION_OBJECT_SUFFIX_MARKERS:
        marker_key = _semantic_key(marker)
        if not normalized.startswith(marker_key):
            continue
        suffix = normalized[len(marker_key):]
        if suffix in {_semantic_key(item) for item in _PRACTICAL_ACTION_OBJECT_SUFFIXES}:
            return marker
    return None


# Strict local safeguard for s3 payoff prose. These stems cover the configured
# imperative families across common Arabic inflections/derivatives without adding
# another model call or a heavyweight morphology dependency.
_S3_FORBIDDEN_ACTION_FAMILY_PATTERNS = (
    r"اختر|اختار|اختيار|يختار|تختار|نختار|مختار",
    r"فعل",
    r"ابد[اأ]|بد[اأ]|بدء|بداي",
    r"كتب",
    r"حدد",
    r"وضع",
    r"حول|تحويل",
    r"ربط",
    r"جرب|تجرب",
    r"اخذ|خذ",
    r"ترك",
    r"جعل",
    r"خصص",
    r"فتح",
    r"غلق|اغلاق",
    r"نفذ|تنفيذ",
    r"خرج",
    r"امش|يمش|تمش|مشي",
    r"حرك",
    r"راقب|مراقب|رقب",
    r"قرا|قراء",
    r"توقف|وقف",
    r"(^|[^\u0600-\u06ff])(قم|قام|قيام|يقوم|تقوم)([^\u0600-\u06ff]|$)",
)


# Run #45 (Telegram, 2026-09-30): Mistral's s3 repair attempt was rejected for
# "فعلا" (the adverb "really/indeed") -- caught only because the "فعل" stem
# pattern above matches as a bare substring, with no word-boundary check, so it
# also fires inside ordinary nouns/adverbs sharing that root that a human would
# never read as a hidden command. These are the specific standalone/prefixed
# forms confirmed safe (not exercised by any locked forbidden-family test,
# which targets "بدأت"/"البداية" via a different pattern left untouched here):
# the noun "situation" (وضع family), the adverb/noun "فعل" family, and the
# common nouns "reading" and "movement" that collide with the قرا/حرك stems.
_S3_FORBIDDEN_ACTION_FAMILY_SAFE_WORDS = frozenset(
    _semantic_key(word)
    for word in (
        "وضع", "الوضع", "بالوضع", "والوضع", "فالوضع", "وضعا", "أوضاع", "الأوضاع",
        "فعلا", "فعليا", "الفعل", "بالفعل", "والفعل", "بفعل", "أفعال", "الأفعال",
        "قراءة", "القراءة", "بالقراءة", "والقراءة",
        "حركة", "الحركة", "بالحركة", "والحركة",
        # Descriptive start-point nouns are not hidden imperatives. Keeping them
        # safe prevents a topic-specific payoff such as "تقارن بدايتك..." from
        # being replaced by unrelated generic anti-procrastination copy.
        "بدايتك", "بدايته", "بدايتها", "بدايتهم", "بدايتنا", "بدايتي",
    )
)


def _contains_forbidden_action_family(text: object) -> bool:
    normalized = _semantic_key(text)
    filtered = " ".join(
        word
        for word in normalized.split()
        if word not in _S3_FORBIDDEN_ACTION_FAMILY_SAFE_WORDS
    )
    return any(re.search(pattern, filtered) for pattern in _S3_FORBIDDEN_ACTION_FAMILY_PATTERNS)


def _sentence_begins_with_direct_action(sentence: object) -> bool:
    normalized = _semantic_key(sentence)
    first = normalized.split()[0] if normalized else ""
    return _practical_action_base(first) is not None


def validate_short_practical_action(value: object) -> str:
    """Validate the Planning-owned single Short action sentence."""
    action = _clean(value)
    if not action:
        raise ShortFormatError("short_practical_action_missing")
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[.!؟!])\s+", action)
        if item.strip()
    ]
    if len(sentences) != 1:
        raise ShortFormatError("short_practical_action_requires_one_sentence")
    sentence = sentences[0]
    if not _sentence_begins_with_direct_action(sentence):
        raise ShortFormatError("short_practical_action_must_begin_with_direct_imperative")
    if _practical_action_marker_count(sentence) != 1:
        raise ShortFormatError("short_practical_action_requires_one_action_only")
    if re.search(r"\s+(?:ثم|و)\s+", sentence):
        raise ShortFormatError("short_practical_action_forbids_joined_second_action")
    if any(
        re.search(_conjoined_practical_action_pattern(marker), sentence, flags=re.I)
        for marker in dict.fromkeys(_PRACTICAL_ACTION_MARKERS)
    ):
        raise ShortFormatError("short_practical_action_forbids_joined_second_action")
    if _SOCIAL_CTA_RE.search(sentence) or _DIALOGUE_LABEL_RE.search(sentence):
        raise ShortFormatError("short_practical_action_invalid_content")
    if _word_count(sentence) > 18:
        raise ShortFormatError("short_practical_action_too_long")
    return sentence


def _salvage_safe_payoff_clause(sentence: object) -> str:
    """Keep only a clearly separated safe descriptive clause from payoff prose."""
    compact = _clean(sentence)
    if not compact or not _contains_forbidden_action_family(compact):
        return ""
    terminal = compact[-1] if compact[-1] in ".!?؟" else "."
    clauses = [
        item.strip(" .!?؟")
        for item in re.split(r"\s*[،,؛;:]\s*", compact)
        if item.strip(" .!?؟")
    ]
    safe = [
        item
        for item in clauses
        if _word_count(item) >= 3
        and _practical_action_marker_count(item) == 0
        and not _contains_forbidden_action_family(item)
    ]
    if not safe:
        return ""
    candidate = "، ".join(safe).strip()
    if not candidate or _contains_forbidden_action_family(candidate):
        return ""
    return candidate + terminal


def _first_sentence(text: object) -> str:
    compact = _clean(text)
    if not compact:
        return ""
    match = _HOOK_SENTENCE_END_RE.search(compact)
    return compact[: match.end()].strip() if match else compact


def _word_count(text: object) -> int:
    return len([word for word in _clean(text).split() if word])


def _safe_locked_payoff_text(payoff_answer: object) -> str:
    """Return a validator-clean local payoff without spending another provider call."""
    fallback = _clean(payoff_answer)
    if (
        _word_count(fallback) < 3
        or _practical_action_marker_count(fallback) != 0
        or _SOCIAL_CTA_RE.search(fallback)
        or _DIALOGUE_LABEL_RE.search(fallback)
    ):
        return ""

    fallback = re.sub(r"[.!؟!]+$", "", fallback).strip() + "."
    if not _contains_forbidden_action_family(fallback):
        return fallback

    salvaged = _salvage_safe_payoff_clause(fallback)
    if salvaged:
        return salvaged

    # Never substitute topic-agnostic copy for a locked semantic payoff.
    # If the locked answer cannot be represented safely, fail closed and let the
    # normal provider/repair path own the wording instead of changing the subject.
    return ""


def apply_safe_short_s3_locked_payoff_fallback(
    script: dict[str, Any],
    payoff_answer: object,
) -> bool:
    """Replace only an unusable payoff with the already-locked descriptive answer."""
    sections = script.get("sections")
    if (
        not isinstance(sections, list)
        or len(sections) != SHORT_SECTION_COUNT
        or not isinstance(sections[2], dict)
    ):
        return False

    fallback = _safe_locked_payoff_text(payoff_answer)
    if not fallback:
        return False

    original = sections[2].get("narration")
    s3 = _clean(original)
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[.!؟!])\s+", s3)
        if item.strip()
    ]
    counts = [_practical_action_marker_count(item) for item in sentences]
    action_indexes = [index for index, count in enumerate(counts) if count]
    if (
        len(action_indexes) != 1
        or counts[action_indexes[0]] != 1
        or not _sentence_begins_with_direct_action(sentences[action_indexes[0]])
    ):
        return False

    action_index = action_indexes[0]
    payoff_sentences = [
        sentence for index, sentence in enumerate(sentences) if index != action_index
    ]
    if not payoff_sentences:
        return False
    if any(not _contains_forbidden_action_family(sentence) for sentence in payoff_sentences):
        return False

    sections[2]["narration"] = f"{fallback} {sentences[action_index]}".strip()
    try:
        validate_short_script(script)
    except ShortFormatError:
        sections[2]["narration"] = original
        return False
    return True


_SAFE_HOOK_TRIM_MAX_OVERRUN = 4
_SAFE_HOOK_TRIM_MIN_WORDS = 10
_SAFE_HOOK_BOUNDARY_CONJUNCTIONS = {"لكن", "ولكن", "و"}
_SAFE_HOOK_SPLIT_CONNECTORS = {
    "لكن", "ولكن", "بل", "لهذا", "لذلك", "إذن", "اذن", "ثم", "وهنا"
}
_SAFE_HOOK_INCOMPLETE_ENDINGS = {
    "في", "من", "إلى", "الى", "على", "عن", "مع", "بلا", "بدون", "دون",
    "قبل", "بعد", "عند", "بين", "خلال", "لدى", "أن", "ان", "إن", "لأن", "لان",
    "حتى", "كي", "ثم", "أو", "او", "بل", "لكن", "و", "إذا", "اذا", "عندما", "حين",
}
_SAFE_HOOK_INCOMPLETE_KEYS = {
    _semantic_key(item) for item in _SAFE_HOOK_INCOMPLETE_ENDINGS
}


def _safe_word_boundary_trim_candidate(
    text: str, *, max_words: int, terminal: str = "."
) -> str | None:
    """Return a conservative local trim only for a 1-4 word overrun past max_words.

    Generalized from the Short hook rescue (PR #955): finds the last natural pause
    point (existing punctuation or a boundary conjunction) at or after
    _SAFE_HOOK_TRIM_MIN_WORDS words in, cuts there, and closes with `terminal`
    (a period for a declarative line like a hook, "؟" for a question).
    """
    words = _clean(text).split()
    overrun = len(words) - max_words
    if overrun < 1 or overrun > _SAFE_HOOK_TRIM_MAX_OVERRUN:
        return None

    candidates: list[int] = []
    ceiling = min(max_words, len(words))
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

        candidate_text = " ".join(kept).strip()
        candidate_text = re.sub(r"[،,؛;:.!?؟!]+$", "", candidate_text).strip()
        if not candidate_text or _word_count(candidate_text) > max_words:
            continue
        return candidate_text + terminal
    return None


def _safe_short_hook_trim_candidate(hook: str) -> str | None:
    """Return a conservative local trim only for a 1-4 word hook overrun."""
    return _safe_word_boundary_trim_candidate(hook, max_words=SHORT_HOOK_MAX_WORDS)


def _safe_short_hook_split_candidate(hook: str) -> tuple[str, str] | None:
    """Split a long provider hook at one natural boundary without deleting meaning."""
    words = _clean(hook).split()
    if not (SHORT_HOOK_RESCUE_MAX_WORDS < len(words) <= SHORT_HOOK_SAFE_SPLIT_MAX_WORDS):
        return None

    candidates: list[tuple[int, bool]] = []
    ceiling = min(SHORT_HOOK_MAX_WORDS, len(words) - 1)
    for index in range(_SAFE_HOOK_TRIM_MIN_WORDS, ceiling + 1):
        word = words[index - 1]
        if re.search(r"[،,؛;:]$", word):
            candidates.append((index, True))
        if index < len(words):
            connector = re.sub(
                r"^[^\w\u0600-\u06ff]+|[^\w\u0600-\u06ff]+$",
                "",
                words[index],
            )
            if _semantic_key(connector) in {
                _semantic_key(item) for item in _SAFE_HOOK_SPLIT_CONNECTORS
            }:
                candidates.append((index, False))

    for cut, punctuation_boundary in reversed(candidates):
        head_words = words[:cut]
        tail_words = words[cut:]
        if not head_words or not tail_words:
            continue
        last = re.sub(r"[^\w\u0600-\u06ff]+$", "", head_words[-1])
        if not last or _semantic_key(last) in _SAFE_HOOK_INCOMPLETE_KEYS:
            continue
        head = " ".join(head_words).strip()
        head = re.sub(r"[،,؛;:.!?؟!]+$", "", head).strip()
        tail = " ".join(tail_words).strip()
        if not head or not tail or _word_count(head) > SHORT_HOOK_MAX_WORDS:
            continue
        first_key = _semantic_key(head).split()[0] if _semantic_key(head).split() else ""
        terminal = "؟" if first_key in {"هل", "لماذا", "كيف", "متى", "اين", "أين", "ماذا"} else "."
        return head + terminal, tail
    return None


def apply_safe_short_hook_split(script: dict[str, Any]) -> bool:
    """Turn one overlong first sentence into hook + continuation at a safe boundary."""
    sections = script.get("sections")
    if not isinstance(sections, list) or not sections or not isinstance(sections[0], dict):
        return False
    narration = _clean(sections[0].get("narration"))
    hook = _first_sentence(narration)
    if not hook or _word_count(hook) <= SHORT_HOOK_RESCUE_MAX_WORDS:
        return False
    split = _safe_short_hook_split_candidate(hook)
    if split is None:
        return False
    head, tail = split
    remainder = narration[len(hook):].lstrip()
    sections[0]["narration"] = " ".join(
        item for item in (head, tail, remainder) if item
    ).strip()
    return True


def safe_word_boundary_trim(text: str, *, max_words: int, terminal: str = ".") -> str | None:
    """Public entry point so other formats can reuse the Short hook rescue heuristic
    (PR #955) for their own word-count ceilings, instead of reinventing it."""
    return _safe_word_boundary_trim_candidate(text, max_words=max_words, terminal=terminal)


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
        len(re.findall(_practical_action_pattern(marker), compact, flags=re.I))
        for marker in unique_markers
    )


# Run #45 (Telegram, 2026-09-30): gemini_flash_lite's repair attempt opened s3's
# action sentence with "لهذا" ("for this reason"), a harmless discourse
# connector not on this list, so the repair helper below couldn't trim it and
# the strict validator rejected the whole script. Widened to cover the other
# single-word connectors a provider is just as likely to reach for in the same
# position; still only ever removed when the prefix is composed entirely of
# words from this set (see the loop below), so an actual clause like "عندما
# تكون مستعدًا" is still correctly left alone.
_SAFE_S3_ACTION_PREFIX_KEYS = frozenset({
    _semantic_key(item)
    for item in (
        "الآن",
        "الان",
        "ثم",
        "لذلك",
        "لذا",
        "لهذا",
        "إذن",
        "اذن",
        "حسنا",
        "طيب",
        "وهنا",
        "هنا",
    )
})


def normalize_short_practical_action(value: object) -> str:
    """Rescue only deterministic shape drift in the Planning-owned Short action.

    This never invents or paraphrases an action. It may remove a harmless
    discourse prefix before the one recognized imperative, or keep only the
    already-valid first action when a provider appends a joined second clause.
    Anything else remains fail-closed in validate_short_practical_action().
    """
    original = _clean(value)
    if not original:
        return original
    try:
        return validate_short_practical_action(original)
    except ShortFormatError:
        pass

    sentences = [
        item.strip()
        for item in re.split(r"(?<=[.!؟!])\s+", original)
        if item.strip()
    ]
    if len(sentences) != 1:
        return original
    sentence = sentences[0]

    spans: list[tuple[int, int]] = []
    for marker in dict.fromkeys(_PRACTICAL_ACTION_MARKERS):
        spans.extend(
            match.span()
            for match in re.finditer(
                _practical_action_pattern(marker), sentence, flags=re.I
            )
        )
    spans = sorted(set(spans))
    if not spans:
        return original

    first_start = spans[0][0]
    if first_start > 0:
        raw_prefix = sentence[:first_start].strip(" \t،,؛;:-")
        prefix_words = [
            _semantic_key(word)
            for word in raw_prefix.split()
            if _semantic_key(word)
        ]
        if not prefix_words or any(
            word not in _SAFE_S3_ACTION_PREFIX_KEYS for word in prefix_words
        ):
            return original
        sentence = sentence[first_start:].lstrip()

    if not _sentence_begins_with_direct_action(sentence):
        return original
    try:
        return validate_short_practical_action(sentence)
    except ShortFormatError:
        pass

    # Run49 class: keep the provider's already-valid first action and discard
    # only a clearly joined tail. The remaining head must independently pass
    # the unchanged strict validator, so this cannot turn ambiguous prose into
    # an accepted action.
    connector = re.search(r"\s+(?:ثم|و)\s+", sentence)
    joined_starts = [
        match.start()
        for marker in dict.fromkeys(_PRACTICAL_ACTION_MARKERS)
        for match in re.finditer(
            _conjoined_practical_action_pattern(marker), sentence, flags=re.I
        )
        if match.start() > 0
    ]
    split_at = (
        min(
            [connector.start()] if connector is not None else []
            + joined_starts
        )
        if connector is not None or joined_starts
        else None
    )
    if split_at is not None:
        head = sentence[:split_at].rstrip(" \t،,؛;:.!?؟!")
        if _word_count(head) >= 3:
            candidate = head + "."
            try:
                return validate_short_practical_action(candidate)
            except ShortFormatError:
                pass

    return original


def apply_safe_short_s3_action_prefix_trim(script: dict[str, Any]) -> bool:
    """Remove only a harmless discourse prefix before the one recognized action."""
    sections = script.get("sections")
    if (
        not isinstance(sections, list)
        or len(sections) != SHORT_SECTION_COUNT
        or not isinstance(sections[2], dict)
    ):
        return False

    original = sections[2].get("narration")
    s3 = _clean(original)
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[.!؟!])\s+", s3)
        if item.strip()
    ]
    counts = [_practical_action_marker_count(item) for item in sentences]
    action_indexes = [index for index, count in enumerate(counts) if count]
    if len(action_indexes) != 1 or counts[action_indexes[0]] != 1:
        return False

    index = action_indexes[0]
    sentence = sentences[index]
    if _sentence_begins_with_direct_action(sentence):
        return False

    spans: list[tuple[int, int]] = []
    for marker in dict.fromkeys(_PRACTICAL_ACTION_MARKERS):
        spans.extend(
            match.span()
            for match in re.finditer(
                _practical_action_pattern(marker), sentence, flags=re.I
            )
        )
    spans = sorted(set(spans))
    if len(spans) != 1:
        return False

    action_start = spans[0][0]
    raw_prefix = sentence[:action_start].strip(" 	،,؛;:-")
    prefix_words = [
        _semantic_key(word)
        for word in raw_prefix.split()
        if _semantic_key(word)
    ]
    if not prefix_words or any(
        word not in _SAFE_S3_ACTION_PREFIX_KEYS for word in prefix_words
    ):
        return False

    repaired_sentence = sentence[action_start:].lstrip()
    if not _sentence_begins_with_direct_action(repaired_sentence):
        return False

    repaired = list(sentences)
    repaired[index] = repaired_sentence
    candidate = " ".join(repaired).strip()
    sections[2]["narration"] = candidate
    try:
        validate_short_script(script)
    except ShortFormatError:
        sections[2]["narration"] = original
        return False
    return True


def apply_locked_short_practical_action(
    script: dict[str, Any],
    locked_action: object,
    *,
    locked_payoff_answer: object = "",
) -> bool:
    """Replace provider-authored Short advice with the one Planning-owned action."""
    action = validate_short_practical_action(locked_action)
    sections = script.get("sections")
    if (
        not isinstance(sections, list)
        or len(sections) != SHORT_SECTION_COUNT
        or not isinstance(sections[2], dict)
    ):
        return False

    original = sections[2].get("narration")
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[.!؟!])\s+", _clean(original))
        if item.strip()
    ]
    payoff: list[str] = []
    for sentence in sentences:
        if _practical_action_marker_count(sentence) > 0:
            continue
        if _contains_forbidden_action_family(sentence):
            salvaged = _salvage_safe_payoff_clause(sentence)
            if salvaged:
                payoff.append(salvaged)
            continue
        payoff.append(sentence)

    if not payoff:
        fallback = _safe_locked_payoff_text(locked_payoff_answer)
        if fallback:
            payoff.append(fallback)
    if not payoff:
        return False

    candidate = " ".join([*payoff, action]).strip()
    sections[2]["narration"] = candidate
    try:
        validate_short_script(script)
    except ShortFormatError:
        sections[2]["narration"] = original
        return False
    return _clean(original) != candidate


def normalize_short_script_candidate(
    script: dict[str, Any],
    *,
    locked_payoff_answer: object = "",
    locked_practical_action: object = "",
) -> dict[str, bool]:
    """Canonical deterministic Short normalization used at every script boundary."""
    hook_trimmed = apply_safe_short_hook_trim(script)
    hook_split = False if hook_trimmed else apply_safe_short_hook_split(script)
    locked_action_applied = False
    if _clean(locked_practical_action):
        locked_action_applied = apply_locked_short_practical_action(
            script,
            locked_practical_action,
            locked_payoff_answer=locked_payoff_answer,
        )
    action_prefix_trimmed = apply_safe_short_s3_action_prefix_trim(script)
    s3_trimmed = apply_safe_short_s3_single_action_trim(script)
    locked_payoff_fallback = apply_safe_short_s3_locked_payoff_fallback(
        script,
        locked_payoff_answer,
    )
    action_prefix_trimmed_after_s3 = apply_safe_short_s3_action_prefix_trim(script)
    return {
        "hook_trimmed": bool(hook_split or hook_trimmed),
        "s3_action_prefix_trimmed": bool(
            action_prefix_trimmed or action_prefix_trimmed_after_s3
        ),
        "s3_trimmed": bool(locked_action_applied or s3_trimmed),
        "s3_locked_payoff_fallback": bool(locked_payoff_fallback),
    }


def apply_safe_short_s3_single_action_trim(script: dict[str, Any]) -> bool:
    """Remove one clearly separated extra s3 command; otherwise stay fail-closed."""
    sections = script.get("sections")
    if (
        not isinstance(sections, list)
        or len(sections) != SHORT_SECTION_COUNT
        or not isinstance(sections[2], dict)
    ):
        return False

    original = sections[2].get("narration")
    s3 = _clean(original)
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[.!؟!])\s+", s3)
        if item.strip()
    ]
    counts = [_practical_action_marker_count(item) for item in sentences]
    action_indexes = [index for index, count in enumerate(counts) if count]
    if len(sentences) < 2 or not any(count == 0 for count in counts):
        return False

    repaired = list(sentences)
    if len(action_indexes) == 1 and counts[action_indexes[0]] == 2:
        index = action_indexes[0]
        sentence = sentences[index]
        spans: list[tuple[int, int]] = []
        for marker in dict.fromkeys(_PRACTICAL_ACTION_MARKERS):
            spans.extend(
                match.span()
                for match in re.finditer(
                    _practical_action_pattern(marker), sentence, flags=re.I
                )
            )
        spans = sorted(set(spans))
        if len(spans) != 2:
            return False
        first_end, second_start = spans[0][1], spans[1][0]
        between = sentence[first_end:second_start]
        separator = re.search(
            r"(?:[،,؛;:]\s*(?:(?:ثم|و)\s*)?|\s+(?:ثم|و)\s+)$",
            between,
        )
        if separator is None:
            return False
        cut = first_end + separator.start()
        if _word_count(sentence[first_end:cut]) < 2:
            return False
        repaired[index] = (
            re.sub(r"[.!؟!]+$", "", sentence[:cut].rstrip(" ،,؛;:")).strip() + "."
        )
    elif len(action_indexes) == 2 and all(
        counts[index] == 1 and _sentence_begins_with_direct_action(sentences[index])
        for index in action_indexes
    ):
        repaired.pop(action_indexes[0])
    elif len(action_indexes) == 2 and all(counts[index] == 1 for index in action_indexes):
        direct_action_indexes = [
            index
            for index in action_indexes
            if _sentence_begins_with_direct_action(sentences[index])
        ]
        extra_action_indexes = [
            index
            for index in action_indexes
            if index not in direct_action_indexes
        ]
        safe_payoff_indexes = [
            index
            for index, count in enumerate(counts)
            if count == 0 and not _contains_forbidden_action_family(sentences[index])
        ]
        if (
            len(direct_action_indexes) != 1
            or len(extra_action_indexes) != 1
            or not safe_payoff_indexes
        ):
            return False
        repaired.pop(extra_action_indexes[0])
    elif (
        len(action_indexes) == 1
        and counts[action_indexes[0]] == 1
        and _sentence_begins_with_direct_action(sentences[action_indexes[0]])
    ):
        action_index = action_indexes[0]
        forbidden_payoff_indexes = [
            index
            for index, sentence in enumerate(sentences)
            if index != action_index and _contains_forbidden_action_family(sentence)
        ]
        safe_payoff_indexes = [
            index
            for index, sentence in enumerate(sentences)
            if index != action_index and not _contains_forbidden_action_family(sentence)
        ]
        if not forbidden_payoff_indexes:
            return False
        if safe_payoff_indexes:
            repaired = [
                sentence
                for index, sentence in enumerate(sentences)
                if index not in forbidden_payoff_indexes
            ]
        else:
            salvaged_by_index = {
                index: _salvage_safe_payoff_clause(sentences[index])
                for index in forbidden_payoff_indexes
            }
            if not any(salvaged_by_index.values()):
                return False
            repaired = []
            for index, sentence in enumerate(sentences):
                if index in forbidden_payoff_indexes:
                    safe_clause = salvaged_by_index.get(index) or ""
                    if safe_clause:
                        repaired.append(safe_clause)
                    continue
                repaired.append(sentence)
    else:
        return False

    candidate = " ".join(repaired).strip()
    sections[2]["narration"] = candidate
    try:
        validate_short_script(script)
    except ShortFormatError:
        sections[2]["narration"] = original
        return False
    return True


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
    if hook_words > SHORT_HOOK_RESCUE_MAX_WORDS:
        raise ShortFormatError(
            f"short_hook_too_long words={hook_words} maximum={SHORT_HOOK_RESCUE_MAX_WORDS}"
        )

    hook_key = _semantic_key(hook)
    if any(
        hook_key == prefix or hook_key.startswith(prefix + " ")
        for prefix in _GREETING_PREFIXES
    ):
        raise ShortFormatError("short_hook_must_not_start_with_greeting")

    tension_shape = validate_short_hook_immediate_tension(hook)

    return {
        "hook": hook,
        "hook_words": hook_words,
        "editorial_maximum_words": SHORT_HOOK_MAX_WORDS,
        "rescue_maximum_words": SHORT_HOOK_RESCUE_MAX_WORDS,
        "rescue_headroom_used": hook_words > SHORT_HOOK_MAX_WORDS,
        "immediate_tension_shape": tension_shape,
    }


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
    s3_sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!؟!])\s+", s3)
        if sentence.strip()
    ]
    action_sentences = [
        sentence
        for sentence in s3_sentences
        if _practical_action_marker_count(sentence) > 0
    ]
    if len(action_sentences) != 1:
        raise ShortFormatError(
            "short_s3_requires_exactly_one_practical_action "
            f"action_sentences={len(action_sentences)}"
        )
    action_sentence = action_sentences[0]
    if not _sentence_begins_with_direct_action(action_sentence):
        raise ShortFormatError("short_s3_action_must_begin_with_direct_imperative")

    action_marker_count = _practical_action_marker_count(action_sentence)
    if action_marker_count != 1:
        raise ShortFormatError(
            "short_s3_requires_one_action_only "
            f"imperative_markers={action_marker_count}"
        )

    payoff_sentences = [sentence for sentence in s3_sentences if sentence != action_sentence]
    if any(_contains_forbidden_action_family(sentence) for sentence in payoff_sentences):
        raise ShortFormatError("short_s3_payoff_contains_forbidden_action_family")

    return {
        "hook": hook,
        "hook_words": hook_words,
        "single_voice": True,
        "social_cta": False,
        "practical_action_sentences": 1,
        "practical_action_markers": action_marker_count,
    }


# First-shot contract is intentionally separate from per-template visual grammar.
# The opening query must name BOTH an observable action/event and a readable tension
# or consequence. Later Short beats are unchanged.
_SHORT_HOOK_VISUAL_ACTION_TERMS = frozenset({
    "stopping", "interrupted", "interrupting", "rushing", "rushed", "running",
    "reaching", "pulling", "pushing", "dropping", "spilling", "tearing", "ripping",
    "opening", "closing", "turning", "leaving", "entering", "checking", "sorting",
    "gripping", "holding", "packing", "unpacking", "typing", "writing", "erasing",
    "crossing", "ringing", "pacing", "hesitating",
})
_SHORT_HOOK_VISUAL_TENSION_TERMS = frozenset({
    "tense", "hesitating", "hesitation", "urgent", "frustrated", "overwhelmed",
    "deadline", "alarm", "pressure", "unfinished", "missed", "late", "blocked",
    "stuck", "broken", "torn", "cracked", "overflowing", "cluttered", "chaotic",
    "interrupted", "conflict", "failure", "failed", "loss", "slipping",
})
_SHORT_HOOK_VISUAL_CALM_GENERIC_PHRASES = (
    "quiet room",
    "calm room",
    "peaceful scene",
    "thoughtful person",
    "person thinking",
    "person sitting",
    "person standing",
    "generic desk",
    "wide landscape",
    "nature landscape",
    "sunset",
    "sunrise",
)


_INNER_DIALOGUE_HOOK_VISUAL_TERMS = frozenset({
    "tense", "hesitating", "hesitation", "rushed", "urgent", "frustrated",
    "overwhelmed", "stopping", "interrupted", "unfinished", "gripping",
    "reaching", "deadline", "alarm", "pressure",
})


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
    "stationery": frozenset({
        "write", "writing", "rewriting", "notebook", "journal", "paper", "page",
        "planner", "checklist", "sticky", "note", "notes", "pen", "pencil",
    }),
    "typing": frozenset({"type", "typing", "keyboard", "laptop", "computer"}),
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


_VISIBLE_FACE_PATTERNS = (
    "portrait",
    "selfie",
    "facial close",
    "face close",
    "close up face",
    "close-up face",
    "looking at camera",
    "smiling face",
)
_FACE_RISK_TOKENS = frozenset({"face", "faces", "facial", "portrait", "selfie", "expression", "expressions"})
_FACE_SAFE_CUES = (
    "no face",
    "no-face",
    "without face",
    "face hidden",
    "hidden face",
    "from behind",
    "back view",
    "hands only",
    "objects only",
    "over shoulder",
    "over-the-shoulder",
)


def _has_explicit_face_risk(query: str) -> bool:
    lowered = _clean(query).casefold()
    if any(cue in lowered for cue in _FACE_SAFE_CUES):
        return False
    tokens = set(re.findall(r"[a-z]+", lowered))
    return bool(
        any(pattern in lowered for pattern in _VISIBLE_FACE_PATTERNS)
        or tokens & _FACE_RISK_TOKENS
    )


def _assert_no_explicit_face_query(query: str) -> None:
    if _has_explicit_face_risk(query):
        raise ShortFormatError("short_visual_query_explicit_face_forbidden")


def _face_safe_query(query: object) -> str:
    compact = _clean(query)
    if not compact or not _has_explicit_face_risk(compact):
        return compact
    safe = compact
    for pattern in sorted(_VISIBLE_FACE_PATTERNS, key=len, reverse=True):
        safe = re.sub(re.escape(pattern), " ", safe, flags=re.I)
    words = [
        word
        for word in safe.split()
        if re.sub(r"[^a-z]+", "", word.casefold()) not in _FACE_RISK_TOKENS
    ]
    safe = " ".join(words).strip(" ,;:-")
    return (safe + " hands only").strip()


def validate_short_hook_visual_query(query: object) -> dict[str, Any]:
    """Fail closed on a calm/generic first shot; later Short beats are untouched."""
    compact = _clean(query)
    words = _query_words(compact)
    action_hits = sorted(words & _SHORT_HOOK_VISUAL_ACTION_TERMS)
    tension_hits = sorted(words & _SHORT_HOOK_VISUAL_TENSION_TERMS)
    lowered = compact.casefold()
    generic_hits = [
        phrase
        for phrase in _SHORT_HOOK_VISUAL_CALM_GENERIC_PHRASES
        if phrase in lowered
    ]

    if not action_hits or not tension_hits:
        if generic_hits:
            raise ShortFormatError("short_visual_query_hook_calm_or_generic")
        raise ShortFormatError("short_visual_query_hook_requires_immediate_tension")
    return {
        "status": "pass",
        "action_hits": action_hits,
        "tension_hits": tension_hits,
        "generic_hits": generic_hits,
    }


def normalize_short_visual_queries(plan: dict[str, Any]) -> bool:
    """Apply only deterministic face-safety and adjacent-family swaps."""
    sections = plan.get("sections")
    if not isinstance(sections, list):
        return False
    changed = False
    prior_families: set[str] = set()
    for raw in sections:
        if not isinstance(raw, dict):
            continue
        primary = _face_safe_query(raw.get("visual_query_en"))
        alternate = _face_safe_query(raw.get("visual_query_alt_en"))
        if primary != _clean(raw.get("visual_query_en")):
            raw["visual_query_en"] = primary
            changed = True
        if alternate != _clean(raw.get("visual_query_alt_en")):
            raw["visual_query_alt_en"] = alternate
            changed = True

        primary_families = _query_action_families(_query_words(primary))
        alternate_families = _query_action_families(_query_words(alternate))
        if (
            prior_families
            and primary_families & prior_families
            and alternate
            and not (alternate_families & prior_families)
        ):
            raw["visual_query_en"], raw["visual_query_alt_en"] = alternate, primary
            primary, alternate = alternate, primary
            primary_families, alternate_families = alternate_families, primary_families
            changed = True
        prior_families = primary_families
    return changed


def validate_short_visual_safety(
    plan: Mapping[str, Any],
    *,
    strict_repetition: bool = False,
) -> dict[str, Any]:
    """Keep true Short visual safety fail-closed; treat family repetition as quality.

    Missing queries and explicit face-risk language are hard Planning failures.
    Consecutive action-family repetition is not: the richer visual_story layer owns
    beat-level diversity and already applies a distinct alternate when available or
    carries a repeat-avoid instruction into downstream Visual QA. Rejecting the whole
    plan here duplicated that logic with a smaller taxonomy and caused valid provider
    plans to die before the stronger visual pipeline could inspect them.
    """
    sections = plan.get("sections")
    if not isinstance(sections, list) or len(sections) != SHORT_SECTION_COUNT:
        raise ShortFormatError("short_visual_safety_requires_three_sections")

    prior_families: set[str] = set()
    repetition_advisories: list[str] = []
    for index, raw in enumerate(sections, start=1):
        if not isinstance(raw, Mapping):
            raise ShortFormatError("short_visual_safety_invalid_section")
        primary = _clean(raw.get("visual_query_en"))
        alternate = _clean(raw.get("visual_query_alt_en"))
        if not primary or not alternate:
            raise ShortFormatError("short_visual_safety_missing_query")
        _assert_no_explicit_face_query(primary)
        _assert_no_explicit_face_query(alternate)

        primary_families = _query_action_families(_query_words(primary))
        alternate_families = _query_action_families(_query_words(alternate))
        if strict_repetition and prior_families and primary_families & prior_families:
            has_distinct_alternate = bool(alternate_families - prior_families)
            if not has_distinct_alternate:
                repetition_advisories.append(f"s{index}")
        prior_families = primary_families

    return {
        "status": "pass",
        "strict_repetition": bool(strict_repetition),
        "repetition_advisories": repetition_advisories,
        "repetition_owner": "visual_story_and_visual_qa",
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
    alternate_queries = [_clean(item.get("visual_query_alt_en")) for item in sections if isinstance(item, Mapping)]
    if len(queries) != SHORT_SECTION_COUNT or any(not query for query in queries):
        raise ShortFormatError("short_visual_query_missing")
    if len(alternate_queries) != SHORT_SECTION_COUNT or any(not query for query in alternate_queries):
        raise ShortFormatError("short_visual_query_alt_missing")
    if any(_semantic_key(primary) == _semantic_key(alternate) for primary, alternate in zip(queries, alternate_queries)):
        raise ShortFormatError("short_visual_query_alt_must_add_new_visual_information")
    for query in (*queries, *alternate_queries):
        _assert_no_explicit_face_query(query)
    hook_visual_report = validate_short_hook_visual_query(queries[0])
    words = [_query_words(query) for query in queries]

    if template == "inner_dialogue":
        allowed = _VISUAL_QUERY_TERMS[template]
        if not (words[0] & (allowed | _INNER_DIALOGUE_HOOK_VISUAL_TERMS)):
            raise ShortFormatError("short_visual_query_inner_dialogue_hook_not_readable")
        if any(not (item & allowed) for item in words[1:]):
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
        "alternate_queries": alternate_queries,
        "action_families": [sorted(_query_action_families(item)) for item in words],
        "hook_visual": hook_visual_report,
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
            "timeline_owner": "measured_charon_voice",
            "editorial_target_seconds": None,
            "minimum_editorial_seconds": None,
            "maximum_editorial_seconds": None,
            "safety_maximum_seconds": SHORT_DURATION_SAFETY_MAX_SECONDS,
        },
        "hook": {
            "first_spoken_sentence": True,
            "maximum_words": SHORT_HOOK_MAX_WORDS,
            "rescue_maximum_words": SHORT_HOOK_RESCUE_MAX_WORDS,
            "rescue_policy": "safe_local_trim_first_then_accept_19_20_only",
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
