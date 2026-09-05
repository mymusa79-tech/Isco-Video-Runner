from __future__ import annotations

"""Runner binding for the Engine-owned Editorial Promise Continuity family.

Engine Tone QA is the sole semantic authority and uses its existing provider call and
RepairDossier. Runner adds no provider request, no retry owner and no second repair
owner. Runner binds immutable viewer intent for standalone Shorts, localizes sibling
Shorts to their exact source semantic job, and verifies that the Engine continuity
capability is present at the existing Tone QA seam before any later hard gate can pass.

The certified Shorts stable port/core stay byte-identical. This family therefore cannot
silently redefine Short finalization or bypass existing Gold/Final-Master ownership.
"""

import re
from functools import wraps
from typing import Any

from isco_video_agent.editorial_room import EditorialContractError, intent_from_dict, make_editorial_intent

from scripts import native_short_planner_router
from scripts import sibling_short_orchestration
from scripts import source_derived_short_planner


SCHEMA_VERSION = 1
_INSTALLED = False
_ENGINE_SEMANTIC_AUTHORITY = "engine_tone_quality_same_provider_call"


class EditorialPromiseContinuityError(RuntimeError):
    pass


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _semantic_key(value: object) -> str:
    text = _clean(value).casefold()
    text = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", text)
    text = text.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"}))
    return " ".join(re.sub(r"[^\w\u0600-\u06ff]+", " ", text).split())


def _sentences(value: object) -> list[str]:
    text = _clean(value)
    if not text:
        return []
    parts = [item.strip() for item in re.split(r"(?<=[.!؟!])\s+", text) if item.strip()]
    return parts or [text]


def _time_context_frame(*values: object) -> list[str]:
    normalized = " ".join(_semantic_key(value) for value in values if _clean(value))
    frames: list[str] = []
    for canonical, markers in (
        ("before", ("قبل",)),
        ("after", ("بعد",)),
        ("during", ("اثناء", "خلال", "بينما")),
    ):
        if any(f" {marker} " in f" {normalized} " for marker in markers):
            frames.append(canonical)
    return frames


def _temporal_conflict(topic: object, action_text: object) -> str | None:
    """Small deterministic tripwire for obvious before/after inversions.

    This is intentionally not the semantic authority. Engine Tone QA owns nuanced
    judgment and repair. The helper exists for regression evidence and metadata only so
    the known Run #198 family cannot disappear from the contract unnoticed.
    """
    topic_frames = set(_time_context_frame(topic))
    action_frames = set(_time_context_frame(action_text))
    if "after" in topic_frames and "after" not in action_frames and "before" in action_frames:
        return "approved_after_context_shifted_to_before_action"
    if "before" in topic_frames and "before" not in action_frames and "after" in action_frames:
        return "approved_before_context_shifted_to_after_action"
    return None


def _standalone_editorial_intent(topic: str, template: str, prior: dict[str, Any]) -> dict[str, Any]:
    topic = _clean(topic)
    if not topic:
        raise EditorialPromiseContinuityError("standalone_short_promise_topic_missing")
    resolved = make_editorial_intent(
        editorial_thesis=(
            f"هذا الشورت يجيب عن المشكلة المحددة في الموضوع المعتمد دون الانتقال إلى مشكلة مجاورة: {topic}"
        ),
        viewer_starting_belief=(
            f"يبدأ المشاهد من السؤال أو القلق المحدد في الموضوع المعتمد نفسه: {topic}"
        ),
        hidden_assumption=(
            "الافتراض الخفي الذي يجب اختباره هو الافتراض الملازم لنفس الموقف، لا افتراض يخص سلوكًا أو مرحلة زمنية أخرى."
        ),
        editorial_turn=(
            f"التحول التحريري يعيد تفسير السؤال نفسه داخل قالب {template} مع بقاء الحدث والسياق والمرحلة الزمنية كما هي."
        ),
        stakes=(
            f"إذا تحولت الإجابة إلى نصيحة مجاورة فسيفقد المشاهد الوعد الذي جعله يدخل هذا الموضوع: {topic}"
        ),
        viewer_promise=(
            f"سيحصل المشاهد على تفسير أو إعادة تأطير مباشر للمشكلة التي يحددها الموضوع المعتمد: {topic}"
        ),
        evidence_boundaries=(
            "لا نضيف ادعاءً دقيقًا غير مدعوم بالمصادر المعتمدة.",
            "لا نغيّر الحدث أو السياق أو المرحلة الزمنية التي يعد بها الموضوع المعتمد.",
        ),
        earned_payoff=(
            f"تنتهي القطعة بخلاصة أو فعل يكتسبه المشاهد من نفس المشكلة التي يطرحها الموضوع المعتمد: {topic}"
        ),
    ).to_dict()
    for key in ("short_template", "short_template_selection", "short_compensation_v2"):
        if key in prior:
            resolved[key] = prior[key]
    resolved["short_promise_contract"] = {
        "schema_version": SCHEMA_VERSION,
        "authority": "approved_topic_before_writing",
        "approved_topic": topic,
        "template": template,
        "required_progression": list(
            (native_short_planner_router._TEMPLATE_COMPENSATION.get(template) or {}).get("beat_shape") or []
        ),
        "time_context_frame": _time_context_frame(topic),
        "same_problem_required": True,
        "adjacent_advice_allowed": False,
        "extra_ai_calls": 0,
    }
    intent_from_dict(resolved)
    return resolved


def _install_standalone_short_promise_contract() -> None:
    """Attach immutable promise metadata without mutating the certified prompt surface.

    Engine Tone QA performs the semantic judgment and the existing RepairDossier owns
    correction. Keeping the planner revision function unchanged preserves the exact
    preflight/runtime prompt-capacity contract and avoids import-order drift.
    """
    current_attach = native_short_planner_router._attach_compensation_metadata
    if getattr(current_attach, "_isco_editorial_promise_continuity", False):
        return

    @wraps(current_attach)
    def attach(plan: object, topic: object, preselected: dict[str, Any]):
        selection = current_attach(plan, topic, preselected)
        prior = getattr(plan, "editorial_intent", None)
        prior = dict(prior) if isinstance(prior, dict) else {}
        template = _clean(selection.get("template"))
        intent = _standalone_editorial_intent(_clean(topic), template, prior)
        action_text = " ".join(
            [
                _clean(getattr(plan, "cta", "")),
                _clean(getattr(plan, "closing_payoff", "")),
            ]
        )
        intent["short_promise_contract"]["deterministic_time_context_warning"] = _temporal_conflict(
            topic,
            action_text,
        )
        setattr(plan, "editorial_intent", intent)
        return selection

    attach._isco_editorial_promise_continuity = True
    attach._isco_editorial_promise_continuity_original = current_attach
    native_short_planner_router._attach_compensation_metadata = attach


def _significant_job_tokens(job: object) -> list[str]:
    stop = {"في", "من", "إلى", "على", "عن", "ما", "هو", "هي", "هذا", "هذه", "ذلك", "تلك", "لماذا", "كيف", "كل"}
    return [token for token in _semantic_key(job).split() if len(token) >= 3 and token not in stop]


def _localized_single_action(job: str, pillar: str) -> str:
    anchor = _clean(job)[:150]
    if pillar == "rise":
        return f"حوّل «{anchor}» إلى خطوة واحدة صغيرة قابلة للتنفيذ اليوم"
    if pillar == "see":
        return f"ارجع إلى موقف يخص «{anchor}» وانظر إليه من الزاوية الجديدة"
    return f"لاحظ موقفًا واحدًا يخص «{anchor}» واسأل ما الذي يفسّره فعلًا"


def _source_local_editorial_intent(request: dict[str, Any]) -> dict[str, Any]:
    job = _clean(request.get("source_semantic_job"))
    excerpt = request.get("source_episode_excerpt")
    if not isinstance(excerpt, dict) or not job:
        raise EditorialPromiseContinuityError("source_short_local_intent_inputs_missing")
    narration = _clean(excerpt.get("source_narration"))
    sentences = _sentences(narration)
    first = (sentences[0] if sentences else job)[:500]
    middle = (sentences[len(sentences) // 2] if sentences else job)[:500]
    last = (sentences[-1] if sentences else job)[:500]
    parent = request.get("source_editorial_intent")
    parent = dict(parent) if isinstance(parent, dict) else {}
    boundaries = [str(item) for item in parent.get("evidence_boundaries", []) if _clean(item)]
    boundaries.append("يلتزم الشورت بالقسم المصدر وحده ولا يوسّع وعد الحلقة الأم أو يضيف مشكلة جديدة.")
    stakes = _clean(parent.get("stakes")) or f"الخروج من الفكرة المحددة سيحوّل الشورت إلى إجابة عن وعد آخر غير {job}."

    intent = make_editorial_intent(
        editorial_thesis=f"هذا الشورت المستقل يشرح الفكرة المحددة في القسم المصدر فقط: {job}",
        viewer_starting_belief=f"يدخل المشاهد من الإطار الذي يفتحه القسم المصدر: {first}",
        hidden_assumption=(
            f"الافتراض الذي يُفحص يجب أن يبقى داخل معنى «{job}» وألا ينتقل إلى مشكلة مجاورة من الحلقة الأم."
        ),
        editorial_turn=f"التحول المحلي يستند إلى نفس القسم ويقدّم زاويته الحاسمة: {middle}",
        stakes=stakes,
        viewer_promise=f"سيحصل المشاهد على فهم مستقل ومكتمل للفكرة المحددة: {job}",
        evidence_boundaries=boundaries,
        earned_payoff=f"يُغلق الشورت على النتيجة التي يكسبها نفس القسم المصدر: {last}",
    ).to_dict()
    intent["source_scope"] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "source_section_local",
        "source_long_topic": _clean(request.get("source_long_topic")),
        "source_semantic_job": job,
        "source_section_id": _clean(excerpt.get("source_section_id")),
        "source_narration_sha256": _clean(excerpt.get("source_narration_sha256")),
        "parent_editorial_intent_preserved_separately": True,
    }
    intent_from_dict(intent)
    return intent


def _source_action_alignment(request: dict[str, Any]) -> dict[str, Any]:
    scope = _clean(request.get("approval_scope"))
    admission = request.get("short_admission") if isinstance(request, dict) else {}
    action = _clean((admission or {}).get("single_action_contract"))
    if scope != "short_sibling":
        return {
            "pass": True,
            "reason": "standalone_or_non_sibling_no_source_action_contract_required",
            "action": action,
        }
    if not action:
        return {"pass": False, "reason": "single_action_contract_missing", "action": ""}
    job = _clean(request.get("source_semantic_job"))
    tokens = _significant_job_tokens(job)
    action_key = set(_semantic_key(action).split())
    overlap = [token for token in tokens if token in action_key]
    return {
        "pass": bool(overlap),
        "reason": "source_job_anchor_present" if overlap else "source_job_anchor_missing",
        "action": action,
        "source_semantic_job": job,
        "matched_tokens": overlap[:8],
    }


def _install_source_short_local_scope() -> None:
    current_requests = sibling_short_orchestration.build_sibling_requests
    if not getattr(current_requests, "_isco_editorial_promise_continuity", False):

        @wraps(current_requests)
        def build_requests(parent_request: dict[str, Any], sibling_plan: dict[str, Any], source_plan: dict[str, Any]):
            requests = current_requests(parent_request, sibling_plan, source_plan)
            for request in requests:
                job = _clean(request.get("source_semantic_job"))
                pillar = _clean((request.get("candidate") or {}).get("pillar")).lower()
                admission = request.get("short_admission")
                if not isinstance(admission, dict):
                    raise EditorialPromiseContinuityError("source_short_admission_missing")
                admission["single_action_contract"] = _localized_single_action(job, pillar)
                admission["single_action_scope"] = "source_semantic_job_local"
                request["source_short_editorial_intent"] = _source_local_editorial_intent(request)
                request["source_short_plan"] = source_derived_short_planner.build_source_short_blueprint(request)
                alignment = _source_action_alignment(request)
                if alignment.get("pass") is not True:
                    raise EditorialPromiseContinuityError(
                        "source_short_single_action_not_bound_to_semantic_job"
                    )
                request["editorial_promise_continuity"] = {
                    "schema_version": SCHEMA_VERSION,
                    "source_action_alignment": alignment,
                    "semantic_authority": _ENGINE_SEMANTIC_AUTHORITY,
                    "provider_calls_added": 0,
                }
                request.pop("request_sha256", None)
                request["request_sha256"] = sibling_short_orchestration._canonical_hash(request)
            return requests

        build_requests._isco_editorial_promise_continuity = True
        build_requests._isco_editorial_promise_continuity_original = current_requests
        sibling_short_orchestration.build_sibling_requests = build_requests

    current_source_intent = source_derived_short_planner._source_editorial_intent
    if not getattr(current_source_intent, "_isco_editorial_promise_continuity", False):

        @wraps(current_source_intent)
        def source_intent(control_request: dict[str, Any]) -> dict[str, Any]:
            local = control_request.get("source_short_editorial_intent")
            if local is None:
                return current_source_intent(control_request)
            if not isinstance(local, dict) or not local:
                raise source_derived_short_planner.SourceDerivedShortError("source_short_editorial_intent_invalid")
            try:
                return intent_from_dict(dict(local)).to_dict()
            except EditorialContractError as exc:
                raise source_derived_short_planner.SourceDerivedShortError(
                    f"source_short_editorial_intent_invalid:{exc}"
                ) from exc

        source_intent._isco_editorial_promise_continuity = True
        source_intent._isco_editorial_promise_continuity_original = current_source_intent
        source_derived_short_planner._source_editorial_intent = source_intent


def require_engine_continuity_evidence(tone: dict[str, Any]) -> dict[str, Any]:
    """Prove that the exact Engine supports this semantic dimension.

    This validates capability/provenance only. It deliberately does NOT translate a
    semantic block into an exception because the existing RepairDossier must still own
    the single repair. Technical provider failures likewise retain Engine's established
    fail-closed tone-audit semantics.
    """
    if not isinstance(tone, dict):
        raise EditorialPromiseContinuityError("engine_continuity_tone_result_invalid")
    evidence = tone.get("editorial_promise_continuity")
    if not isinstance(evidence, dict):
        raise EditorialPromiseContinuityError("engine_continuity_evidence_missing")
    if evidence.get("semantic_authority") != _ENGINE_SEMANTIC_AUTHORITY:
        raise EditorialPromiseContinuityError("engine_continuity_authority_invalid")
    if evidence.get("provider_calls_added") != 0:
        raise EditorialPromiseContinuityError("engine_continuity_provider_budget_invalid")
    if evidence.get("repair_owner") != "existing_tone_repair_dossier":
        raise EditorialPromiseContinuityError("engine_continuity_repair_owner_invalid")
    validation = _clean(evidence.get("validation"))
    if not validation:
        raise EditorialPromiseContinuityError("engine_continuity_validation_missing")
    decision = _clean(evidence.get("decision"))
    if decision not in {"pass", "block"}:
        raise EditorialPromiseContinuityError("engine_continuity_decision_invalid")
    flags = evidence.get("flags")
    if not isinstance(flags, list):
        raise EditorialPromiseContinuityError("engine_continuity_flags_invalid")
    if decision == "pass" and (validation != "valid" or flags):
        raise EditorialPromiseContinuityError("engine_continuity_pass_evidence_inconsistent")
    if any(not str(item).startswith("editorial_promise_continuity:") for item in flags):
        raise EditorialPromiseContinuityError("engine_continuity_flag_prefix_invalid")
    return evidence


def install_editorial_promise_continuity() -> None:
    """Install intent bindings only; semantic QA and repair remain Engine-owned."""
    global _INSTALLED
    if _INSTALLED:
        return
    _install_standalone_short_promise_contract()
    _install_source_short_local_scope()
    _INSTALLED = True
    print(
        "Editorial Promise Continuity V1 installed: "
        "semantic_authority=Engine Tone QA; Long+StandaloneShort+SiblingShort; "
        "provider_calls_added=0; repair_owner=existing_tone_repair_dossier; "
        "stable_short_port_and_core=unchanged"
    )
