from __future__ import annotations

import ast
import hashlib
import json
import re
from functools import wraps
from pathlib import Path
from typing import Any

import isco_video_agent.orchestrator as orchestrator


REPORT_FILENAME = "producer-handoff-quality.json"
SCHEMA_VERSION = 1

# Keep the provider-facing Producer instruction semantically complete but transport-
# compact. The deterministic acceptance rules below remain the authority; this text is
# guidance, not a replacement for any factuality/template/representation gate.
_PRODUCER_DIRECTIVE = (
    "Producer pre-gate: precise factual/high-risk claims require APPROVED_RESEARCH_PACK; otherwise use modest non-technical "
    "wording. Use specific natural MSA; non-diagnostic, non-preachy, no generic AI motivation. Preserve narrative/template and "
    "distinct beats. Moment: no direct commands or serialized-list on_screen_text; show template progression."
)

_SHORT_TEMPLATE_CONTRACTS = {
    "why_reframe": (
        "why_reframe must visibly progress from a mistaken assumption to an explicit contrast, then a reflective reframe, "
        "then a useful non-preachy payoff. Do not return one static definition dressed as four metadata beats."
    ),
    "inner_dialogue": (
        "inner_dialogue must visibly progress through an inner thought, friction, a turn in perspective, and a useful payoff; "
        "keep it intimate without fabricated autobiography or melodrama."
    ),
    "micro_story": (
        "micro_story must visibly progress through a concrete scene/event, change, turn, and meaning/payoff; do not replace "
        "the progression with generic advice."
    ),
    "quote_reflection": (
        "quote_reflection may use only quotation evidence already present in the approved topic/research, followed by reflection "
        "and payoff; never invent or alter a quotation."
    ),
}

_SHORT_IMPERATIVE_RE = re.compile(
    r"^(?:قل|قولي|افعل|افعلي|توقف|توقفي|ابدأ|ابدئي|تذكر|تذكري|واجه|واجهي|اختر|اختاري|"
    r"اترك|اتركي|كن|كوني|جرب|جربي|حاول|حاولي|تخلص|تخلصي|لا\s+تسمح|لا\s+تسمحي|لا\s+تخف|لا\s+تخافي)\b",
    flags=re.IGNORECASE,
)

_GENERIC_SHORT_PHRASES = (
    "ثق بنفسك",
    "لا تستسلم",
    "أنت أقوى مما تظن",
    "ابدأ الآن",
    "كل شيء ممكن",
    "رحلتك تبدأ الآن",
)

_WHY_REFRAME_MARKERS = (
    "لكن",
    "بل",
    "المشكلة ليست",
    "الأدق",
    "في الواقع",
    "الحقيقة أن",
    "بينما",
    "بدلا من",
    "بدلًا من",
    "أحيانا",
    "أحيانًا",
)

_EMPTY_RESEARCH_HIGH_RISK_PATTERNS = (
    re.compile(r"\b\d+(?:[.,]\d+)?\s*%"),
    re.compile(r"(?:ثبت\s+علمي|أثبتت\s+الدراسات|تشير\s+الدراسات|الدراسات\s+(?:تثبت|تؤكد))", re.IGNORECASE),
    re.compile(
        r"(?:يسبب|يؤدي\s+إلى|يرفع|يخفض|يزيد|يقلل)\s+.{0,48}(?:الدوبامين|الكورتيزول|السيروتونين|"
        r"هرمون|الدماغ|اضطراب|اكتئاب|الذاكرة|الجهاز\s+العصبي)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:يعالج|علاج|تشخيص)\s+.{0,40}(?:اضطراب|اكتئاب|قلق|مرض|حالة)", re.IGNORECASE),
)

_INSTALLED_PLANNING = False
_INSTALLED_HANDOFF = False


class ProducerQualityContractError(RuntimeError):
    pass


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _semantic_key(value: object) -> str:
    text = _clean(value).casefold()
    text = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", text)
    text = text.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"}))
    return " ".join(re.sub(r"[^\w\u0600-\u06ff]+", " ", text).split())


def _research_pack(context: dict | None) -> list[Any]:
    source = context if isinstance(context, dict) else {}
    for key in ("approved_research_pack", "research_pack"):
        value = source.get(key)
        if isinstance(value, list):
            return value
    return []


def producer_writing_directive(research_context: dict | None = None) -> str:
    evidence = "present" if _research_pack(research_context) else "EMPTY"
    return f"{_PRODUCER_DIRECTIVE} APPROVED_RESEARCH_PACK={evidence}."


def short_template_contract(template: object) -> str:
    return _SHORT_TEMPLATE_CONTRACTS.get(_clean(template), "")


def _template_from_plan(plan: object) -> str:
    intent = getattr(plan, "editorial_intent", None)
    if isinstance(intent, dict):
        template = _clean(intent.get("short_template"))
        if template:
            return template
    narrative = _clean(getattr(plan, "narrative_format", ""))
    if narrative.startswith("short_"):
        return narrative.removeprefix("short_")
    return ""


def _looks_serialized_list(value: object) -> bool:
    text = str(value or "").strip()
    if not (text.startswith("[") and text.endswith("]")):
        return False
    try:
        parsed = ast.literal_eval(text)
    except (SyntaxError, ValueError):
        return False
    return isinstance(parsed, list)


def _plan_text_fields(plan: object) -> list[str]:
    values = [
        _clean(getattr(plan, "topic", "")),
        _clean(getattr(plan, "hook", "")),
        _clean(getattr(plan, "cta", "")),
        _clean(getattr(plan, "closing_payoff", "")),
    ]
    values.extend(_clean(item) for item in list(getattr(plan, "title_options", []) or []))
    for section in list(getattr(plan, "sections", []) or []):
        values.extend(
            [
                _clean(getattr(section, "narration", "")),
                _clean(getattr(section, "on_screen_text", "")),
                _clean(getattr(section, "key_point", "")),
            ]
        )
    return [value for value in values if value]


def _short_story_beats(plan: object) -> list[str]:
    sections = list(getattr(plan, "sections", []) or [])
    first = sections[0] if sections else None
    titles = list(getattr(plan, "title_options", []) or [])
    return [
        _clean(getattr(plan, "hook", "")),
        _clean(titles[0] if titles else ""),
        _clean(getattr(first, "on_screen_text", "") if first is not None else "")
        or _clean(getattr(first, "key_point", "") if first is not None else ""),
        _clean(getattr(plan, "closing_payoff", "")),
    ]


def plan_quality_issues(
    plan: object,
    *,
    research_context: dict | None = None,
    short_template_override: str | None = None,
) -> list[str]:
    issues: list[str] = []
    fmt = _clean(getattr(plan, "format", "")).lower()
    sections = list(getattr(plan, "sections", []) or [])
    if not sections:
        issues.append("plan_has_no_sections")
        return issues

    for index, section in enumerate(sections, 1):
        on_screen = getattr(section, "on_screen_text", "")
        if _looks_serialized_list(on_screen):
            issues.append(f"section_{index}_on_screen_text_serialized_list")
        if not _clean(getattr(section, "visual_query", "")):
            issues.append(f"section_{index}_visual_query_empty")

    if not _research_pack(research_context):
        joined = "\n".join(_plan_text_fields(plan))
        if any(pattern.search(joined) for pattern in _EMPTY_RESEARCH_HIGH_RISK_PATTERNS):
            issues.append("unsupported_precise_claim_without_approved_research")

    if fmt == "moment":
        if len(sections) != 1:
            issues.append("moment_requires_exactly_one_section")
        if _clean(getattr(sections[0], "narration", "")):
            issues.append("moment_narration_must_be_empty")

        beats = [value for value in _short_story_beats(plan) if value]
        normalized = [_semantic_key(value) for value in beats]
        if len(set(normalized)) < min(3, len(normalized)):
            issues.append("moment_story_beats_not_distinct")

        viewer_story_fields = [
            _clean(getattr(plan, "hook", "")),
            _clean(getattr(sections[0], "on_screen_text", "")),
            _clean(getattr(plan, "closing_payoff", "")),
        ]
        if any(_SHORT_IMPERATIVE_RE.search(value) for value in viewer_story_fields if value):
            issues.append("moment_direct_imperative_in_story_beat")
        joined_story = " ".join(viewer_story_fields)
        if any(phrase in joined_story for phrase in _GENERIC_SHORT_PHRASES):
            issues.append("moment_generic_motivation_phrase")

        template = _clean(short_template_override) or _template_from_plan(plan)
        if template == "why_reframe":
            post_hook = " ".join(_short_story_beats(plan)[1:])
            if not any(marker in post_hook for marker in _WHY_REFRAME_MARKERS):
                issues.append("why_reframe_missing_explicit_contrast_or_reframe")

    if fmt in {"film", "story"}:
        keys = [_semantic_key(getattr(section, "key_point", "")) for section in sections]
        keys = [value for value in keys if value]
        if len(keys) != len(set(keys)):
            issues.append("long_form_duplicate_key_points")

    return issues


def validate_plan_for_producer_handoff(
    plan: object,
    *,
    research_context: dict | None = None,
    short_template_override: str | None = None,
) -> object:
    issues = plan_quality_issues(
        plan,
        research_context=research_context,
        short_template_override=short_template_override,
    )
    if issues:
        raise ProducerQualityContractError("producer_plan_handoff_blocked:" + ",".join(issues))
    return plan


def merge_producer_revision_note(existing: object, research_context: dict | None) -> str:
    """Compose the Producer pre-gate with any existing planning requirement."""
    prior = _clean(existing)
    directive = producer_writing_directive(research_context)
    if directive in prior:
        return prior
    return f"{prior} Producer pre-gate requirement: {directive}" if prior else directive


# Backward-compatible private name for already-installed callers.
_merge_revision_note = merge_producer_revision_note


def install_planning_producer_quality_contract() -> None:
    """Constrain initial writing and every repair, then validate before independent text audits."""
    global _INSTALLED_PLANNING
    current = orchestrator.build_plan
    if getattr(current, "_isco_producer_quality_contract", False):
        _INSTALLED_PLANNING = True
        return

    @wraps(current)
    def wrapped(*args, **kwargs):
        research_context = kwargs.get("research_context")
        updated = dict(kwargs)
        updated["revision_note"] = merge_producer_revision_note(
            updated.get("revision_note", ""),
            research_context,
        )
        plan = current(*args, **updated)
        return validate_plan_for_producer_handoff(plan, research_context=research_context)

    wrapped._isco_producer_quality_contract = True
    wrapped._isco_producer_quality_original = current
    orchestrator.build_plan = wrapped
    _INSTALLED_PLANNING = True
    print("Producer Quality Contract installed: constrained writing + deterministic pre-audit plan handoff")


def _read_json(path: Path, expected: type) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ProducerQualityContractError(f"producer_handoff_invalid_json:{path.name}") from exc
    if not isinstance(value, expected):
        raise ProducerQualityContractError(f"producer_handoff_wrong_shape:{path.name}")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require(checks: dict[str, bool], name: str, condition: bool) -> None:
    checks[name] = bool(condition)
    if not condition:
        raise ProducerQualityContractError(f"producer_handoff_failed:{name}")


def certify_producer_handoff(output_dir: Path) -> dict[str, Any]:
    """Cheap stage acceptance before independent Audio Integrity -> Final Master QC -> Gold."""
    root = Path(output_dir)
    plan = _read_json(root / "plan.json", dict)
    quality = _read_json(root / "quality-final.json", dict)
    rights = _read_json(root / "rights-manifest.json", dict)
    monetization = _read_json(root / "monetization-check.json", dict)
    visual_audit = _read_json(root / "visual-audit.json", list)
    final_path = root / "final.mp4"
    checks: dict[str, bool] = {}

    _require(checks, "final_master_exists", final_path.is_file() and final_path.stat().st_size > 1024)
    _require(checks, "duration_stage_certified", quality.get("duration_ok") is True)
    _require(checks, "video_stream_single", int(quality.get("video_streams") or 0) == 1)

    sections = plan.get("sections") if isinstance(plan.get("sections"), list) else []
    _require(checks, "plan_sections_present", bool(sections))
    _require(checks, "visual_audit_present", bool(visual_audit))
    reviewed = int(quality.get("visual_sections_reviewed") or 0)
    _require(checks, "visual_section_coverage", reviewed >= len(sections))
    _require(checks, "rights_manifest_present", isinstance(rights.get("visuals"), list) and bool(rights.get("visuals")))
    _require(
        checks,
        "monetization_precheck_passed",
        str(monetization.get("status") or "").upper() == "PASS_WITH_UPLOAD_ACTIONS",
    )

    fmt = str(plan.get("format") or quality.get("format") or "").strip().lower()
    short_finished = (root / "short-intelligence-pre-gold.json").is_file()
    phase = "short_finished" if fmt == "moment" and short_finished else "core_render"

    if fmt != "moment" or short_finished:
        _require(checks, "audio_stream_single", int(quality.get("audio_streams") or 0) == 1)
        _require(checks, "audio_stage_certified", quality.get("audio_ok") is True)
        _require(checks, "av_sync_stage_certified", quality.get("av_sync_ok") is True)

    if fmt != "moment":
        mastering = _read_json(root / "audio-mastering.json", dict)
        _require(checks, "long_audio_mastering_applied", mastering.get("status") == "applied")

    if fmt == "moment" and short_finished:
        short_state = _read_json(root / "short-intelligence-pre-gold.json", dict)
        _require(checks, "short_voice_quality_refresh", quality.get("short_voice_v2_refresh") is True)
        _require(checks, "short_voice_rights", isinstance(rights.get("short_voice_v2"), dict))
        compensation = short_state.get("compensation") if isinstance(short_state.get("compensation"), dict) else {}
        if compensation.get("beat_driven_multi_shot_applied") is True:
            timeline = _read_json(root / "short-visual-timeline.json", dict)
            shot_count = int(timeline.get("shot_count") or 0)
            distinct = int(timeline.get("distinct_asset_count") or 0)
            _require(checks, "short_multi_shot_present", shot_count >= 2)
            _require(checks, "short_final_assets_distinct", distinct == shot_count)
            _require(checks, "short_cinematic_rights", isinstance(rights.get("short_cinematic_v1"), dict))

    receipt = {
        "phase": phase,
        "format": fmt,
        "decision": "pass",
        "checks": checks,
        "final_sha256": _sha256_file(final_path),
        "independent_gates_next": ["audio_semantic_integrity", "final_master_qc", "gold"],
        "extra_ai_calls": 0,
    }
    report_path = root / REPORT_FILENAME
    report = {"schema_version": SCHEMA_VERSION, "decision": "pass", "receipts": []}
    if report_path.is_file():
        try:
            existing = json.loads(report_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                report = existing
        except Exception:
            pass
    receipts = [item for item in list(report.get("receipts") or []) if isinstance(item, dict) and item.get("phase") != phase]
    receipts.append(receipt)
    report.update({"schema_version": SCHEMA_VERSION, "decision": "pass", "receipts": receipts})
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Producer handoff PASS: phase={phase} checks={len(checks)} extra_ai_calls=0")
    return receipt


def install_producer_handoff_contract(production_modules: list[Any]) -> None:
    """Make producer acceptance the outer check before independent final gates."""
    global _INSTALLED_HANDOFF
    if _INSTALLED_HANDOFF:
        return
    installed = 0
    for production in production_modules:
        current = getattr(production, "run_final_master_qc", None)
        if not callable(current) or getattr(current, "_isco_producer_handoff_contract", False):
            continue

        def make_wrapper(original):
            @wraps(original)
            def wrapped(output_dir: Path, *args, **kwargs):
                certify_producer_handoff(Path(output_dir))
                return original(output_dir, *args, **kwargs)

            wrapped._isco_producer_handoff_contract = True
            wrapped._isco_producer_handoff_original = original
            return wrapped

        production.run_final_master_qc = make_wrapper(current)
        installed += 1
    if installed <= 0:
        raise ProducerQualityContractError("producer_handoff_final_master_qc_binding_missing")
    _INSTALLED_HANDOFF = True
