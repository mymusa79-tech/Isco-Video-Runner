from __future__ import annotations

import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()

# Production fix: the observe-only failure report must be writable even when
# EditorialIntent validation fails before any provider call.
p = root / "src/isco_video_agent/production_pipeline.py"
s = p.read_text(encoding="utf-8")
old = '''    reports = report_dir or output_dir
    try:
        editorial_identity = editorial_learning_identity(plan)
        reports.mkdir(parents=True, exist_ok=True)
'''
new = '''    reports = report_dir or output_dir
    reports.mkdir(parents=True, exist_ok=True)
    try:
        editorial_identity = editorial_learning_identity(plan)
'''
assert s.count(old) == 1, "Stage 8 reports/identity patch point changed"
s = s.replace(old, new, 1)
p.write_text(s, encoding="utf-8")

intent_helper = '''\n\ndef _intent():
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
'''

# Legacy Final Critic provenance fixtures now exercise the mandatory Stage 8
# EditorialIntent contract instead of being rejected before their original
# budget/provenance assertions can run.
p = root / "tests/test_final_critic_provenance_batch.py"
s = p.read_text(encoding="utf-8")
old = "from isco_video_agent.learning import record_metrics\nfrom isco_video_agent.models import ProductionPlan, ScriptSection\n\n\ndef _plan() -> ProductionPlan:\n"
new = "from isco_video_agent.learning import record_metrics\nfrom isco_video_agent.editorial_room import make_editorial_intent\nfrom isco_video_agent.models import ProductionPlan, ScriptSection\n" + intent_helper + "\n\ndef _plan() -> ProductionPlan:\n"
assert s.count(old) == 1, "provenance import/helper patch point changed"
s = s.replace(old, new, 1)
old = '''        narrative_format="journey_upward",\n    )\n'''
new = '''        narrative_format="journey_upward",\n        editorial_intent=_intent().to_dict(),\n    )\n'''
assert s.count(old) == 1, "provenance plan patch point changed"
s = s.replace(old, new, 1)
p.write_text(s, encoding="utf-8")

# Gold Shadow remains observer-only, but its direct _run_final_critic fixture
# must also satisfy the now-mandatory EditorialIntent contract.
p = root / "tests/test_gold_shadow_phase2a.py"
s = p.read_text(encoding="utf-8")
old = "from isco_video_agent.ai_budget import BudgetLedger\nfrom isco_video_agent.gold_finalizer import observe_gold_output\n\n\ndef _write_release_inputs(out: Path) -> None:\n"
new = "from isco_video_agent.ai_budget import BudgetLedger\nfrom isco_video_agent.editorial_room import make_editorial_intent\nfrom isco_video_agent.gold_finalizer import observe_gold_output\n" + intent_helper + "\n\ndef _write_release_inputs(out: Path) -> None:\n"
assert s.count(old) == 1, "gold shadow import/helper patch point changed"
s = s.replace(old, new, 1)
old = '''    return SimpleNamespace(\n        format="film",\n        hook="hook",\n'''
new = '''    return SimpleNamespace(\n        format="film",\n        editorial_intent=_intent().to_dict(),\n        hook="hook",\n'''
assert s.count(old) == 1, "gold shadow plan patch point changed"
s = s.replace(old, new, 1)
p.write_text(s, encoding="utf-8")

# Regression test for the real ordering bug: an invalid intent in observe-only
# mode must fail safely before providers while still persisting its namespaced
# diagnostic report.
p = root / "tests/test_editorial_stage8.py"
s = p.read_text(encoding="utf-8")
marker = '''\n\nif __name__ == "__main__":\n    unittest.main()\n'''
insert = '''\n    def test_observe_only_invalid_intent_creates_namespaced_failure_report_without_provider_call(self):
        plan = _plan()
        plan.editorial_intent = {}
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            report_dir = out / "gold-shadow" / "phase2a"
            with patch("isco_video_agent.production_pipeline._ledger_call_status") as provider_call:
                result = _run_final_critic(
                    output_dir=out,
                    plan=plan,
                    gemini="key",
                    content_model="model",
                    release_mode="observe_only",
                    report_dir=report_dir,
                )
            provider_call.assert_not_called()
            self.assertEqual(result["observation_status"], "failed_observation")
            self.assertTrue(result["would_block_if_enforced"])
            self.assertTrue((report_dir / "final-critic.json").exists())
'''
assert s.count(marker) == 1, "Stage 8 test insertion point changed"
s = s.replace(marker, insert + marker, 1)
p.write_text(s, encoding="utf-8")
