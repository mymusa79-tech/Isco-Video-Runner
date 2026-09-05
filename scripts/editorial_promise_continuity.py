from __future__ import annotations

"""One editorial-promise continuity family for Long, standalone Short, and sibling Short.

This layer deliberately reuses the existing independent Tone QA provider call and the
existing RepairDossier. It adds no provider request, no new repair owner, and never
weakens an existing gate. Its job is to keep the approved viewer problem, context/time
frame, reframe/payoff, single action, and visual intent on the same editorial promise.
"""

import json
import re
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import Any

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.tone_quality as engine_tone
from isco_video_agent.editorial_room import EditorialContractError, intent_from_dict, make_editorial_intent

from scripts import native_short_planner_router
from scripts import shorts_production_binding as short_core
from scripts import sibling_short_orchestration
from scripts import source_derived_short_planner


SCHEMA_VERSION = 1
_PROMPT_MARKER = "[EDITORIAL_PROMISE_CONTINUITY_V1]"
_LOCK = RLock()
_INSTALLED = False


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


def _plan_contract(plan: object) -> dict[str, Any]:
    sections = []
    for section in list(getattr(plan, "sections", []) or [])[:10]:
        sections.append(
            {
                "id": _clean(getattr(section, "id", ""))[:40],
                "key_point": _clean(getattr(section, "key_point", ""))[:320],
                "on_screen_text": _clean(getattr(section, "on_screen_text", ""))[:320],
                "visual_query": _clean(getattr(section, "visual_query", ""))[:320],
                "emotion": _clean(getattr(section, "emotion", ""))[:80],
            }
        )
    editorial = getattr(plan, "editorial_intent", None)
    if not isinstance(editorial, dict):
        editorial = {}
    return {
        "schema_version": SCHEMA_VERSION,
        "topic": _clean(getattr(plan, "topic", ""))[:500],
        "format": _clean(getattr(plan, "format", ""))[:40],
        "hook": _clean(getattr(plan, "hook", ""))[:700],
        "cta": _clean(getattr(plan, "cta", ""))[:700],
        "closing_payoff": _clean(getattr(plan, "closing_payoff", ""))[:700],
        "editorial_intent": editorial,
        "sections": sections,
        "time_context_frame": _time_context_frame(
            getattr(plan, "topic", ""),
            getattr(plan, "hook", ""),
        ),
    }


def _append_continuity_contract(prompt: str, plan: object) -> str:
    if _PROMPT_MARKER in prompt:
        return prompt
    contract = json.dumps(_plan_contract(plan), ensure_ascii=False, separators=(",", ":"))
    return (
        prompt
        + "\n\n"
        + _PROMPT_MARKER
        + "\n"
        + "Additional mandatory editorial-continuity audit. Do not rewrite. Judge semantic fulfillment, not word overlap.\n"
        + "1. The approved topic/title promise, hook, section or beat job, CTA, closing payoff and visual_query must stay on the SAME viewer problem and the same causal/context/time frame.\n"
        + "2. A fluent adjacent piece of advice is still a defect if it answers another question. Concrete known failure example: a promise about replaying every word AFTER a meeting must not drift into advice to pause BEFORE replying.\n"
        + "3. A deeper explanation, natural paraphrase, or practical action is valid when it directly earns the SAME promised outcome; do not require literal keyword copying.\n"
        + "4. If editorial_intent is a full contract, actual viewer-facing material must execute viewer_starting_belief, hidden_assumption, editorial_turn, viewer_promise and earned_payoff. Merely echoing metadata is not fulfillment.\n"
        + "5. If short_promise_contract exists, approved_topic, selected template, required progression and time/context frame are locked intent.\n"
        + "6. A visual may be metaphorical, but it must support the same event/emotion/conceptual job. Generic keyword symbolism alone is insufficient when it changes the situation promised to the viewer.\n"
        + "7. Put each continuity defect in the EXISTING narrative_format_flags array and prefix it exactly with editorial_promise_continuity:. Do not add keys or change the response schema.\n"
        + "8. Any such flag is blocking and must be eligible for the existing Tone RepairDossier; do not soften it into notes.\n"
        + f"LOCKED_CONTINUITY_CONTRACT={contract}\n"
        + "[/EDITORIAL_PROMISE_CONTINUITY_V1]"
    )


class _OpenRouterPromptProxy:
    def __init__(self, original_module: object, plan: object):
        self._original_module = original_module
        self._plan = plan

    def json_text(self, prompt: str, *args, **kwargs):
        return self._original_module.json_text(
            _append_continuity_contract(prompt, self._plan),
            *args,
            **kwargs,
        )


def _install_tone_continuity_adapter() -> None:
    current = orchestrator.audit_tone_and_naturalness
    if getattr(current, "_isco_editorial_promise_continuity", False):
        return

    @wraps(current)
    def wrapped(api_key: str, plan: object, model: str):
        # Engine Tone QA already owns provider routing, diagnostics, Approval-Shopping
        # behavior and fail-closed semantics. Decorate only the prompt at its two
        # provider seams and restore them even if the audit raises.
        with _LOCK:
            original_json_text = engine_tone.json_text
            original_openrouter = engine_tone.openrouter

            def routed_json_text(key: str, prompt: str, *, model: str):
                return original_json_text(
                    key,
                    _append_continuity_contract(prompt, plan),
                    model=model,
                )

            engine_tone.json_text = routed_json_text
            engine_tone.openrouter = _OpenRouterPromptProxy(original_openrouter, plan)
            try:
                result = current(api_key, plan, model)
            finally:
                engine_tone.json_text = original_json_text
                engine_tone.openrouter = original_openrouter
        if isinstance(result, dict):
            flags = [
                str(item)
                for item in result.get("narrative_format_flags", [])
                if str(item).startswith("editorial_promise_continuity:")
            ]
            result["editorial_promise_continuity"] = {
                "schema_version": SCHEMA_VERSION,
                "decision": "block" if flags else "pass",
                "flags": flags,
                "provider_calls_added": 0,
                "repair_owner": "existing_tone_repair_dossier",
            }
        return result

    wrapped._isco_editorial_promise_continuity = True
    wrapped._isco_editorial_promise_continuity_original = current
    orchestrator.audit_tone_and_naturalness = wrapped


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
    # Canonicalize now so an invalid locally constructed intent fails before media.
    intent_from_dict(resolved)
    return resolved


def _install_standalone_short_promise_contract() -> None:
    current_attach = native_short_planner_router._attach_compensation_metadata
    if not getattr(current_attach, "_isco_editorial_promise_continuity", False):
        @wraps(current_attach)
        def attach(plan: object, topic: object, preselected: dict[str, Any]):
            selection = current_attach(plan, topic, preselected)
            prior = getattr(plan, "editorial_intent", None)
            prior = dict(prior) if isinstance(prior, dict) else {}
            template = _clean(selection.get("template"))
            setattr(plan, "editorial_intent", _standalone_editorial_intent(_clean(topic), template, prior))
            return selection

        attach._isco_editorial_promise_continuity = True
        attach._isco_editorial_promise_continuity_original = current_attach
        native_short_planner_router._attach_compensation_metadata = attach

    current_revision = native_short_planner_router.merge_short_template_revision
    if not getattr(current_revision, "_isco_editorial_promise_continuity", False):
        @wraps(current_revision)
        def revision(template: str, existing: object) -> str:
            base = current_revision(template, existing)
            requirement = (
                "Editorial promise continuity is mandatory: every hook, contrast/turn, reframe, payoff and CTA must answer "
                "the exact USER_TOPIC_DATA problem and preserve its event, causal context and time frame. Useful adjacent advice "
                "does not satisfy the promise. Visual intent must support the same situation, not only a generic keyword metaphor."
            )
            return f"{base} Additional continuity requirement: {requirement}"

        revision._isco_editorial_promise_continuity = True
        revision._isco_editorial_promise_continuity_original = current_revision
        native_short_planner_router.merge_short_template_revision = revision
        # Historical alias is used by a few direct tests/callers.
        native_short_planner_router._planning_revision_note = revision


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


def _temporal_conflict(topic: object, action_text: object) -> str | None:
    topic_frames = set(_time_context_frame(topic))
    action_frames = set(_time_context_frame(action_text))
    if "after" in topic_frames and "after" not in action_frames and "before" in action_frames:
        return "approved_after_context_shifted_to_before_action"
    if "before" in topic_frames and "before" not in action_frames and "after" in action_frames:
        return "approved_before_context_shifted_to_after_action"
    return None


def _source_action_alignment(request: dict[str, Any]) -> dict[str, Any]:
    admission = request.get("short_admission") if isinstance(request, dict) else {}
    action = _clean((admission or {}).get("single_action_contract"))
    if not action:
        return {"pass": False, "reason": "single_action_contract_missing", "action": ""}
    scope = _clean(request.get("approval_scope"))
    if scope != "short_sibling":
        return {"pass": True, "reason": "standalone_or_non_sibling", "action": action}
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


def _install_final_short_delivery_guard() -> None:
    current = short_core.finalize_short_quality
    if getattr(current, "_isco_editorial_promise_continuity", False):
        return

    @wraps(current)
    def finalize(output_dir: Path, control_request: dict[str, Any], pre_gold: dict[str, Any]):
        report = current(output_dir, control_request, pre_gold)
        root = Path(output_dir)
        plan = short_core._read(root / "plan.json")
        tone = short_core._read(root / "tone-quality-audit.json")
        flags = [
            str(item)
            for item in tone.get("narrative_format_flags", [])
            if str(item).startswith("editorial_promise_continuity:")
        ]
        if tone.get("status") != "pass" or flags:
            raise EditorialPromiseContinuityError("short_delivery_continuity_tone_gate_failed")

        action_alignment = _source_action_alignment(control_request)
        if action_alignment.get("pass") is not True:
            raise EditorialPromiseContinuityError(
                "short_delivery_single_action_not_bound_to_source_semantic_job"
            )

        topic = _clean(control_request.get("approved_topic") or plan.get("topic"))
        action_text = " ".join(
            [
                _clean((control_request.get("short_admission") or {}).get("single_action_contract")),
                _clean(plan.get("cta")),
                _clean(plan.get("closing_payoff")),
            ]
        )
        temporal = _temporal_conflict(topic, action_text)
        if temporal:
            raise EditorialPromiseContinuityError("short_delivery_temporal_promise_drift:" + temporal)

        continuity = {
            "schema_version": SCHEMA_VERSION,
            "decision": "pass",
            "continuity_flags": [],
            "semantic_authority": "independent_tone_audit_same_provider_call",
            "provider_calls_added": 0,
            "repair_flow": "existing_tone_repair_dossier",
            "temporal_alignment": {"pass": True, "topic_frames": _time_context_frame(topic)},
            "single_action_alignment": action_alignment,
        }
        report["editorial_promise_continuity"] = continuity
        provenance = report.setdefault("evidence_provenance", {})
        provenance["promise_payoff_semantic_authority"] = "editorial_promise_continuity"
        provenance["promise_payoff_numeric_score_role"] = "existing_final_critic_craft_proxy_not_semantic_authority"
        provenance["semantic_promise_score_fabricated"] = False
        (root / "short-intelligence.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return report

    finalize._isco_editorial_promise_continuity = True
    finalize._isco_editorial_promise_continuity_original = current
    short_core.finalize_short_quality = finalize


def install_editorial_promise_continuity() -> None:
    """Install one family without adding a provider/retry/repair owner."""
    global _INSTALLED
    if _INSTALLED:
        return
    _install_standalone_short_promise_contract()
    _install_source_short_local_scope()
    _install_tone_continuity_adapter()
    _install_final_short_delivery_guard()
    _INSTALLED = True
    print(
        "Editorial Promise Continuity V1 installed: "
        "Long+StandaloneShort+SiblingShort; provider_calls_added=0; "
        "repair_owner=existing_tone_repair_dossier; delivery=fail_closed"
    )
