from __future__ import annotations

import sys
from pathlib import Path


root = Path(sys.argv[1] if len(sys.argv) > 1 else "engine")

editorial_learning = '''from __future__ import annotations

from .editorial_room import EditorialContractError, intent_from_dict
from .models import ProductionPlan

_FORBIDDEN_AUTHORITIES = ("safety", "factuality", "rights", "cultural")
_SHORT_FORMATS = {"moment", "short"}


def editorial_learning_identity(plan: ProductionPlan) -> dict:
    """Return a deterministic, soft-only EditorialIntent learning identity."""
    data = plan.editorial_intent
    if not isinstance(data, dict) or not data:
        raise EditorialContractError("editorial_intent_missing")
    supplied_fingerprint = str(data.get("editorial_fingerprint", "")).strip()
    if not supplied_fingerprint:
        raise EditorialContractError("editorial_intent_fingerprint_missing")
    intent = intent_from_dict(data)
    if supplied_fingerprint != intent.editorial_fingerprint:
        raise EditorialContractError("editorial_intent_fingerprint_mismatch")
    kind = "short" if str(plan.format).strip().lower() in _SHORT_FORMATS else "long"
    return {
        "schema_version": 1,
        "editorial_fingerprint": intent.editorial_fingerprint,
        "persona_version": intent.persona_version,
        "cohort_kind": kind,
        "cohort_id": f"{kind}:{intent.editorial_fingerprint}",
        "policy": "soft_only",
        "automatic_policy_influence": False,
        "forbidden_authorities": list(_FORBIDDEN_AUTHORITIES),
        "intent_contract": {
            "viewer_promise": intent.viewer_promise,
            "editorial_turn": intent.editorial_turn,
            "earned_payoff": intent.earned_payoff,
        },
    }


def validate_editorial_learning_identity(identity: dict, *, format_hint: str | None = None) -> dict:
    if not isinstance(identity, dict):
        raise ValueError("editorial learning identity must be a dict")
    fingerprint = str(identity.get("editorial_fingerprint", "")).strip()
    persona_version = str(identity.get("persona_version", "")).strip()
    cohort_kind = str(identity.get("cohort_kind", "")).strip().lower()
    cohort_id = str(identity.get("cohort_id", "")).strip()
    if not fingerprint or not persona_version or cohort_kind not in {"long", "short"}:
        raise ValueError("invalid editorial learning identity")
    if cohort_id != f"{cohort_kind}:{fingerprint}":
        raise ValueError("editorial learning cohort mismatch")
    if identity.get("policy") != "soft_only" or identity.get("automatic_policy_influence") is not False:
        raise ValueError("editorial learning must remain soft-only")
    forbidden = tuple(str(x) for x in identity.get("forbidden_authorities", []))
    if forbidden != _FORBIDDEN_AUTHORITIES:
        raise ValueError("editorial learning authority boundary mismatch")
    if format_hint:
        expected = "short" if format_hint.strip().lower() in _SHORT_FORMATS else "long"
        if cohort_kind != expected:
            raise ValueError("editorial learning format cohort mismatch")
    contract = identity.get("intent_contract", {})
    if not isinstance(contract, dict) or any(
        not str(contract.get(key, "")).strip()
        for key in ("viewer_promise", "editorial_turn", "earned_payoff")
    ):
        raise ValueError("editorial intent fulfillment contract missing")
    return identity
'''
(root / "src/isco_video_agent/editorial_learning.py").write_text(editorial_learning, encoding="utf-8")

p = root / "src/isco_video_agent/learning.py"
s = p.read_text(encoding="utf-8")
old = "def learning_context(format: str | None = None) -> dict:\n"
new = "def learning_context(format: str | None = None, *, editorial_identity: dict | None = None) -> dict:\n"
assert s.count(old) == 1
s = s.replace(old, new, 1)

marker = "\ndef record_feedback(\n"
insert = '''
def record_editorial_identity(output: str, editorial_identity: dict) -> bool:
    """Attach soft-only editorial cohort metadata to an already accepted production."""
    from .editorial_learning import validate_editorial_learning_identity

    target = output.strip()
    if not target:
        return False
    identity = validate_editorial_learning_identity(editorial_identity)
    data = load_history()
    videos = data.get("videos", [])
    if not isinstance(videos, list):
        return False
    changed = False
    for item in reversed(videos):
        if (
            isinstance(item, dict)
            and str(item.get("output", "")) == target
            and item.get("release_status") == "accepted_after_final_critic"
        ):
            item["editorial_learning"] = {
                "schema_version": identity["schema_version"],
                "editorial_fingerprint": identity["editorial_fingerprint"],
                "persona_version": identity["persona_version"],
                "cohort_kind": identity["cohort_kind"],
                "cohort_id": identity["cohort_id"],
                "policy": "soft_only",
                "automatic_policy_influence": False,
                "forbidden_authorities": list(identity["forbidden_authorities"]),
            }
            changed = True
            break
    if changed:
        _save_history(data)
    return changed


'''
assert s.count(marker) == 1
s = s.replace(marker, insert + marker, 1)

old_return = '''    return {
        "policy": "Learning may tune creative preferences only. It cannot override cultural, factuality, rights, monetization, security, user-approval, or zero-cost gates.",
        "reinforce": reinforce[-20:],
        "avoid": avoid[-20:],
        "recent_human_feedback": recent_notes,
        "recent_metrics": metrics,
    }
'''
new_return = '''    result = {
        "policy": "Learning may tune creative preferences only. It cannot override cultural, factuality, rights, monetization, security, user-approval, or zero-cost gates.",
        "reinforce": reinforce[-20:],
        "avoid": avoid[-20:],
        "recent_human_feedback": recent_notes,
        "recent_metrics": metrics,
    }
    if editorial_identity is not None:
        from .editorial_learning import validate_editorial_learning_identity

        identity = validate_editorial_learning_identity(editorial_identity, format_hint=requested or None)
        cohort_id = identity["cohort_id"]
        videos = data.get("videos", [])
        cohort_history = []
        if isinstance(videos, list):
            cohort_history = [
                dict(item.get("editorial_learning", {}))
                for item in videos
                if isinstance(item, dict)
                and item.get("release_status") == "accepted_after_final_critic"
                and isinstance(item.get("editorial_learning"), dict)
                and item["editorial_learning"].get("cohort_id") == cohort_id
            ][-6:]
        result["editorial_identity"] = {
            "schema_version": identity["schema_version"],
            "editorial_fingerprint": identity["editorial_fingerprint"],
            "persona_version": identity["persona_version"],
            "cohort_kind": identity["cohort_kind"],
            "cohort_id": cohort_id,
            "policy": "soft_only",
            "automatic_policy_influence": False,
            "forbidden_authorities": list(identity["forbidden_authorities"]),
        }
        result["editorial_cohort_history"] = cohort_history
        result["editorial_intent_contract"] = dict(identity["intent_contract"])
    return result
'''
assert s.count(old_return) == 1
s = s.replace(old_return, new_return, 1)
p.write_text(s, encoding="utf-8")

p = root / "src/isco_video_agent/final_critic.py"
s = p.read_text(encoding="utf-8")
old = '''_CREATIVE_LEARNING_KEYS = (
    "policy",
    "reinforce",
    "avoid",
    "recent_human_feedback",
)
'''
new = '''_CREATIVE_LEARNING_KEYS = (
    "policy",
    "reinforce",
    "avoid",
    "recent_human_feedback",
    "editorial_identity",
    "editorial_cohort_history",
    "editorial_intent_contract",
)
'''
assert s.count(old) == 1
s = s.replace(old, new, 1)
old = '''- LEARNING_CONTEXT may tune creative preference only; it can NEVER override safety, cultural, factuality, rights,
  monetization, approval, or zero-cost rules.
- VISUAL_AUDITS_SELECTED_ONLY contains only footage that is actually in the final cut: exactly one primary winner per
'''
new = '''- LEARNING_CONTEXT may tune creative preference only; it can NEVER override safety, cultural, factuality, rights,
  monetization, approval, or zero-cost rules.
- If EDITORIAL_INTENT_CONTRACT is present inside LEARNING_CONTEXT, it is the locked creative promise for this episode.
  Verify that the final narration actually fulfills viewer_promise, visibly executes editorial_turn, and earns earned_payoff.
  Put any material failure to fulfill that contract in critical_issues. Do not invent a new promise.
- VISUAL_AUDITS_SELECTED_ONLY contains only footage that is actually in the final cut: exactly one primary winner per
'''
assert s.count(old) == 1
s = s.replace(old, new, 1)
old = '''    return {
        "status": status,
        "hard_blocks": hard_blocks,
        "model_review": model_review,
'''
new = '''    intent_contract = creative_learning_context.get("editorial_intent_contract", {})
    identity = creative_learning_context.get("editorial_identity", {})
    return {
        "status": status,
        "hard_blocks": hard_blocks,
        "model_review": model_review,
        "editorial_intent_fulfillment": {
            "policy": "same_final_release_critic_call",
            "evaluated": bool(intent_contract),
            "editorial_fingerprint": str(identity.get("editorial_fingerprint", "")),
            "persona_version": str(identity.get("persona_version", "")),
            "viewer_promise": str(intent_contract.get("viewer_promise", "")),
            "editorial_turn": str(intent_contract.get("editorial_turn", "")),
            "earned_payoff": str(intent_contract.get("earned_payoff", "")),
            "failure_channel": "model_review.critical_issues",
            "additional_model_calls": 0,
        },
'''
assert s.count(old) == 1
s = s.replace(old, new, 1)
p.write_text(s, encoding="utf-8")

p = root / "src/isco_video_agent/production_pipeline.py"
s = p.read_text(encoding="utf-8")
old = '''from .final_critic import audit_final_release
from .gold_finalizer import finalize_gold_output
from .learning import learning_context, mark_production_accepted, remove_production_record
'''
new = '''from .editorial_learning import editorial_learning_identity
from .final_critic import audit_final_release
from .gold_finalizer import finalize_gold_output
from .learning import learning_context, mark_production_accepted, record_editorial_identity, remove_production_record
'''
assert s.count(old) == 1
s = s.replace(old, new, 1)
old = '''    reports = report_dir or output_dir
    try:
        reports.mkdir(parents=True, exist_ok=True)
'''
new = '''    reports = report_dir or output_dir
    try:
        editorial_identity = editorial_learning_identity(plan)
        reports.mkdir(parents=True, exist_ok=True)
'''
assert s.count(old) == 1
s = s.replace(old, new, 1)
old = '''            opening_visual_audit=opening_visual_audit,
            learning_context=learning_context(plan.format),
            model=content_model,
'''
new = '''            opening_visual_audit=opening_visual_audit,
            learning_context=learning_context(plan.format, editorial_identity=editorial_identity),
            model=content_model,
'''
assert s.count(old) == 1
s = s.replace(old, new, 1)
old = '''    )

    # Read-only YouTube learning is deliberately last: it cannot affect planning,
'''
new = '''    )

    # Editorial fingerprint/cohort tracking is soft-only metadata recorded after acceptance.
    try:
        record_editorial_identity(output_key, editorial_learning_identity(plan))
    except Exception:
        pass

    # Read-only YouTube learning is deliberately last: it cannot affect planning,
'''
assert s.count(old) == 1
s = s.replace(old, new, 1)
p.write_text(s, encoding="utf-8")

tests = '''from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from isco_video_agent.anti_repetition import append_history
from isco_video_agent.editorial_learning import editorial_learning_identity
from isco_video_agent.editorial_room import EditorialContractError, make_editorial_intent
from isco_video_agent.final_critic import audit_final_release
from isco_video_agent.learning import learning_context, mark_production_accepted, record_editorial_identity
from isco_video_agent.models import ProductionPlan, ScriptSection
from isco_video_agent.production_pipeline import _run_final_critic


def _intent():
    return make_editorial_intent(
        editorial_thesis="العودة بعد السقوط تُبنى من خطوة صغيرة قابلة للتكرار.",
        viewer_starting_belief="المشاهد يظن أن السقوط يعني أن عليه العودة دفعة واحدة.",
        hidden_assumption="الافتراض الخفي أن الخطوات الصغيرة لا تُحسب كعودة حقيقية.",
        editorial_turn="بدل انتظار قفزة كبيرة، ابدأ بخطوة صغيرة قابلة للتنفيذ اليوم.",
        stakes="انتظار العودة الكاملة قد يطيل التوقف ويضعف الثقة.",
        viewer_promise="ستتعلم كيف تبدأ العودة بعد السقوط بخطوة صغيرة قابلة للتنفيذ اليوم.",
        evidence_boundaries=("لا نقدم علاجًا طبيًا.", "لا نعمم مسارًا واحدًا على الجميع."),
        earned_payoff="اختر خطوة صغيرة قابلة للتنفيذ اليوم وابدأ منها عودتك.",
    )


def _plan(fmt="film"):
    intent = _intent()
    return ProductionPlan(
        topic="العودة بعد السقوط",
        pillar="act",
        format=fmt,
        hook="السقوط لا يطلب منك قفزة.",
        title_options=["كيف تبدأ العودة؟"],
        thumbnail_concepts=["خطوة أولى"],
        sections=[ScriptSection(id="s1", narration="ابدأ بخطوة صغيرة قابلة للتنفيذ اليوم ثم كررها.", visual_query="single step")],
        cta="ابدأ اليوم",
        closing_payoff="خطوة واحدة اليوم تكفي لتبدأ.",
        editorial_intent=intent.to_dict(),
    )


def _good_review():
    return {
        "human_feel": 0.95,
        "language_quality": 0.95,
        "opening_strength": 0.95,
        "narrative_progression": 0.95,
        "cultural_fit": 0.98,
        "originality": 0.95,
        "monetization_safety": 0.98,
        "critical_issues": [],
        "improvements": [],
        "summary": "fulfilled",
    }


class EditorialStage8Tests(unittest.TestCase):
    def test_long_and_short_cohorts_are_separate_and_soft_only(self):
        long_id = editorial_learning_identity(_plan("film"))
        short_id = editorial_learning_identity(_plan("moment"))
        self.assertEqual(long_id["cohort_kind"], "long")
        self.assertEqual(short_id["cohort_kind"], "short")
        self.assertTrue(long_id["cohort_id"].startswith("long:"))
        self.assertTrue(short_id["cohort_id"].startswith("short:"))
        self.assertEqual(long_id["policy"], "soft_only")
        self.assertIs(long_id["automatic_policy_influence"], False)
        self.assertEqual(long_id["forbidden_authorities"], ["safety", "factuality", "rights", "cultural"])

    def test_tampered_fingerprint_fails_closed(self):
        plan = _plan()
        plan.editorial_intent = dict(plan.editorial_intent)
        plan.editorial_intent["editorial_fingerprint"] = "0" * 64
        with self.assertRaises(EditorialContractError):
            editorial_learning_identity(plan)

    def test_learning_context_returns_only_same_editorial_cohort_without_policy_influence(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "history.json"
            with patch.dict(os.environ, {"ISCO_HISTORY_PATH": str(state)}, clear=False):
                long_plan = _plan("film")
                short_plan = _plan("moment")
                long_id = editorial_learning_identity(long_plan)
                short_id = editorial_learning_identity(short_plan)
                append_history({"topic": "long", "output": "output/long/final.mp4"})
                self.assertTrue(mark_production_accepted("output/long/final.mp4"))
                self.assertTrue(record_editorial_identity("output/long/final.mp4", long_id))
                append_history({"topic": "short", "output": "output/short/final.mp4"})
                self.assertTrue(mark_production_accepted("output/short/final.mp4"))
                self.assertTrue(record_editorial_identity("output/short/final.mp4", short_id))
                ctx = learning_context("film", editorial_identity=long_id)
                self.assertEqual(len(ctx["editorial_cohort_history"]), 1)
                self.assertEqual(ctx["editorial_cohort_history"][0]["cohort_kind"], "long")
                self.assertEqual(ctx["reinforce"], [])
                self.assertEqual(ctx["avoid"], [])
                self.assertIn("cannot override", ctx["policy"])

    def test_final_critic_uses_same_call_for_locked_intent_fulfillment(self):
        plan = _plan()
        identity = editorial_learning_identity(plan)
        ctx = learning_context(plan.format, editorial_identity=identity)
        visual = [{"section": "s1", "is_selected": True, "status": "pass"}]
        with patch("isco_video_agent.final_critic.json_text", return_value=_good_review()) as model_call:
            result = audit_final_release(
                "key",
                plan=plan,
                quality={"duration_ok": True, "audio_ok": True},
                visual_audits=visual,
                rights_manifest={"visuals": [{"provider": "pexels"}]},
                monetization_check={"status": "PASS_WITH_UPLOAD_ACTIONS"},
                opening_visual_audit={"status": "pass"},
                learning_context=ctx,
                model="model",
            )
        self.assertEqual(model_call.call_count, 1)
        prompt = model_call.call_args.args[1]
        self.assertIn(identity["intent_contract"]["viewer_promise"], prompt)
        self.assertIn(identity["intent_contract"]["editorial_turn"], prompt)
        self.assertIn(identity["intent_contract"]["earned_payoff"], prompt)
        self.assertIn(identity["editorial_fingerprint"], prompt)
        fulfillment = result["editorial_intent_fulfillment"]
        self.assertTrue(fulfillment["evaluated"])
        self.assertEqual(fulfillment["additional_model_calls"], 0)
        self.assertEqual(fulfillment["failure_channel"], "model_review.critical_issues")

    def test_enforcing_pipeline_rejects_invalid_intent_before_any_provider_call(self):
        plan = _plan()
        plan.editorial_intent = {}
        with tempfile.TemporaryDirectory() as td:
            with patch("isco_video_agent.production_pipeline._ledger_call_status") as provider_call:
                with self.assertRaises(EditorialContractError):
                    _run_final_critic(
                        output_dir=Path(td),
                        plan=plan,
                        gemini="key",
                        content_model="model",
                        release_mode="enforce",
                    )
                provider_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
'''
(root / "tests/test_editorial_stage8.py").write_text(tests, encoding="utf-8")
