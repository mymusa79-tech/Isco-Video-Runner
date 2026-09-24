from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from clean_v2.pipeline import (
    _closing_payoff_for_tone_audit,
    _factuality_location_issue_notes,
    _factuality_repair_issue_notes,
    _factuality_target_section_ids,
    _first_spoken_sentence,
    _repair_target_section_ids,
    _run_legacy_factuality_audit,
    _run_legacy_tone_naturalness_audit,
    _run_one_bounded_tone_repair,
    _run_text_audits,
)
from clean_v2.tone_audit import (
    TONE_AUDIT_SCHEMA,
    _mistral_tone_call,
    _scope_clean_v2_tone_prompt,
    _scope_religious_quote_prompt,
    _enforce_hook_quality_contract,
)


def _tone_result(*, status: str = "pass", validation: str = "valid") -> dict:
    return {
        "status": status,
        "validation": validation,
        "provider": "gemini" if validation == "valid" else None,
        "attempts": [],
        "preachiness_flags": [],
        "cultural_dignity_flags": [],
        "naturalness_flags": [],
        "narrative_format_flags": [],
        "unverified_religious_quote_flags": [],
        "hook_specificity": True,
        "hook_honesty": True,
        "hook_curiosity": True,
        "hook_genericness": False,
        "notes": [],
    }


class CleanV2ToneNaturalnessTests(unittest.TestCase):
    def test_strict_schema_matches_legacy_tone_contract(self):
        self.assertFalse(TONE_AUDIT_SCHEMA["additionalProperties"])
        self.assertEqual(
            set(TONE_AUDIT_SCHEMA["required"]),
            {
                "status",
                "preachiness_flags",
                "cultural_dignity_flags",
                "naturalness_flags",
                "narrative_format_flags",
                "unverified_religious_quote_flags",
                "hook_specificity",
                "hook_honesty",
                "hook_curiosity",
                "hook_genericness",
                "notes",
            },
        )
        self.assertEqual(
            TONE_AUDIT_SCHEMA["properties"]["status"]["enum"],
            ["pass", "block"],
        )

    def test_mistral_tone_call_uses_strict_schema(self):
        payload = _tone_result()
        captured = {}

        def fake_executor(prompt, **kwargs):
            captured["prompt"] = prompt
            captured.update(kwargs)
            return payload

        with patch("clean_v2.tone_audit.mistral_executor_json", side_effect=fake_executor), patch(
            "clean_v2.tone_audit._validate_tone_result",
            side_effect=lambda value: value,
        ):
            result = _mistral_tone_call("audit prompt")

        self.assertEqual(result, payload)
        self.assertEqual(captured["task_kind"], "text_audit")
        self.assertEqual(captured["temperature"], 0.1)
        name, schema = captured["response_schema"]
        self.assertEqual(name, "clean_v2_tone_naturalness_audit_v2")
        self.assertIs(schema, TONE_AUDIT_SCHEMA)

    def test_hook_quality_prompt_adds_same_call_editorial_dimensions(self):
        base = (
            "5. Unverified religious quotations: flag any religious quotation or attribution presented as authoritative unless the\n"
            "   approved research context directly supports it as verified. Judge this semantically - do not rely only on a fixed\n"
            "   list of marker phrases."
        )
        scoped = _scope_clean_v2_tone_prompt(base)
        for field in (
            "hook_specificity",
            "hook_honesty",
            "hook_curiosity",
            "hook_genericness",
        ):
            self.assertIn(field, scoped)
        self.assertIn("calm hooks are fully acceptable", scoped)
        self.assertIn("dozens of unrelated videos", scoped)
        self.assertIn("SAME audit response", scoped)

    def test_generic_hook_example_is_rejected(self):
        payload = _tone_result()
        payload.update(
            {
                "hook_specificity": False,
                "hook_honesty": False,
                "hook_curiosity": False,
                "hook_genericness": True,
                "notes": ["hook=غيّر حياتك اليوم."],
            }
        )
        result = _enforce_hook_quality_contract(payload)
        self.assertEqual(result["status"], "block")
        hook_flags = [
            item
            for item in result["narrative_format_flags"]
            if item.startswith("hook_quality:")
        ]
        self.assertEqual(len(hook_flags), 1)
        self.assertIn("hook_specificity", hook_flags[0])
        self.assertIn("hook_genericness", hook_flags[0])

    def test_specific_quiet_hook_example_is_accepted(self):
        payload = _tone_result()
        payload["notes"] = ["hook=لماذا تنتهي خطتك كل يوم عند أول مقاطعة؟"]
        result = _enforce_hook_quality_contract(payload)
        self.assertEqual(result["status"], "pass")
        self.assertFalse(
            any(
                item.startswith("hook_quality:")
                for item in result["narrative_format_flags"]
            )
        )

    def test_first_spoken_sentence_is_runtime_hook(self):
        script = {
            "sections": [
                {
                    "id": "s1",
                    "narration": "لماذا نخطط كثيرًا ولا نبدأ؟ الجواب ليس نقص الوقت.",
                }
            ]
        }
        self.assertEqual(
            _first_spoken_sentence(script),
            "لماذا نخطط كثيرًا ولا نبدأ؟",
        )

    def test_run254_tone_bridge_uses_actual_closing_payoff_and_identity(self):
        script = {
            "sections": [
                {"id": "s1", "narration": "هوك فعلي واضح. ثم بداية الشرح."},
                {
                    "id": "s5",
                    "narration": (
                        "اختر إشارة واحدة واضحة وجرّبها غدًا. "
                        "راقب هل قرّبت خطتك من الواقع بدل الأمل. "
                        "هذه هي الخلاصة التي نريد أن تبقى. "
                        "إلى لقاء جديد مع اليقظة، وحفظكم الله."
                    ),
                },
            ]
        }
        identity = {
            "opener": "افتتاح الهوية",
            "closer": "إلى لقاء جديد مع اليقظة، وحفظكم الله.",
            "transitions": ["أولاً", "ثم", "أخيرًا"],
        }
        payoff = _closing_payoff_for_tone_audit(script, identity=identity)
        self.assertNotIn(identity["closer"], payoff)
        self.assertIn("قرّبت خطتك من الواقع بدل الأمل", payoff)
        self.assertIn("هذه هي الخلاصة", payoff)

        captured = {}
        dummy_plan = SimpleNamespace(
            hook="",
            closing_payoff="وعد التخطيط الذي لا يجب تقييمه كخاتمة",
            identity_opener="",
            identity_closer="",
            identity_transitions=[],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "narrative-identity.json").write_text(
                json.dumps(identity, ensure_ascii=False),
                encoding="utf-8",
            )

            def audit(_api_key, production_plan, _model):
                captured["plan"] = production_plan
                return _tone_result()

            with patch(
                "clean_v2.pipeline._build_production_plan_for_audit",
                return_value=dummy_plan,
            ), patch(
                "clean_v2.tone_audit.audit_tone_and_naturalness_with_mistral",
                side_effect=audit,
            ):
                _run_legacy_tone_naturalness_audit(
                    output_dir=root,
                    brief={"format": "film"},
                    plan={"promise": "وعد التخطيط"},
                    script=script,
                )

        plan = captured["plan"]
        self.assertEqual(plan.hook, "هوك فعلي واضح.")
        self.assertEqual(plan.closing_payoff, payoff)
        self.assertNotEqual(plan.closing_payoff, "وعد التخطيط")
        self.assertEqual(plan.identity_opener, identity["opener"])
        self.assertEqual(plan.identity_closer, identity["closer"])
        self.assertEqual(plan.identity_transitions, identity["transitions"])

    def test_run256_tone_scope_defers_visual_query_and_respects_host_locks(self):
        base = (
            "5. Unverified religious quotations: flag any religious quotation or attribution presented as authoritative unless the\n"
            "   approved research context directly supports it as verified. Judge this semantically - do not rely only on a fixed\n"
            "   list of marker phrases."
        )
        scoped = _scope_clean_v2_tone_prompt(base)
        self.assertIn("spoken-text audit", scoped)
        self.assertIn("Do not block on visual_query", scoped)
        self.assertIn("contextual CTA anchor section is host-owned", scoped)
        self.assertIn("Do not require moving the CTA to a", scoped)
        self.assertIn("different section or to the ending", scoped)
        self.assertIn("narrative identity opener/closer are host-owned", scoped)

    def test_run254_religious_quote_scope_keeps_invocation_distinct_from_quote(self):
        base = (
            "5. Unverified religious quotations: flag any religious quotation or attribution presented as authoritative unless the\n"
            "   approved research context directly supports it as verified. Judge this semantically - do not rely only on a fixed\n"
            "   list of marker phrases."
        )
        scoped = _scope_religious_quote_prompt(base)
        self.assertIn("بسم الله / باسم الله / حفظكم الله", scoped)
        self.assertIn("are not quotations or attributions by themselves", scoped)
        self.assertIn("unless they actually quote or attribute", scoped)
        self.assertEqual(scoped.count("Scope clarification for Clean V2"), 1)

    def test_short_cohort_attempt_1_factuality_note_resolves_s2(self):
        script = {
            "sections": [
                {
                    "id": "s1",
                    "narration": "أحياناً يختفي الدافع كأنك في منتصف نهارٍ صامت.",
                },
                {
                    "id": "s2",
                    "narration": "لكن الحقيقة أن توقعاتنا للنتيجة تُشغِّل مشاعرنا أكثر من الطاقة نفسها.",
                },
                {
                    "id": "s3",
                    "narration": "ابدأ بتدوين هدف صغير اليوم، ثم اكتب خطوة واحدة لتحقيقه.",
                },
            ]
        }
        report = {
            "status": "block",
            "unsupported_claims": [{"section_id": "s2", "issue": "claim that expectations influence emotions more than energy"}],
            "diagnostics": {"raw_result": {"unsupported_claims": [{"section_id": "s2", "issue": "claim that expectations influence emotions more than energy"}], "professional_advice_flags": [], "expert_persona_flags": []}},
            "professional_advice_flags": [],
            "expert_persona_flags": [],
            "notes": ["Section s2 contains an unsupported psychological claim."],
        }

        factuality_notes = _factuality_repair_issue_notes(report)
        location_notes = _factuality_location_issue_notes(report, script)
        revision_note = "\n".join(
            item for item in (factuality_notes, location_notes) if item
        )

        self.assertEqual(location_notes, "- [factuality-location] s2")
        self.assertEqual(
            _factuality_target_section_ids(report, script),
            ("s2",),
        )

    def test_short_cohort_attempt_1_structured_id_resolves_s2_without_prose_hint(self):
        script = {
            "sections": [
                {
                    "id": "s1",
                    "narration": "لا تنتظر أن يعود الدافع، لأنه لن يأتي بمفرده.",
                },
                {
                    "id": "s2",
                    "narration": "الافتراض الخفي هو أن الحركة تحتاج إلى حماس أولًا، لكن العكس هو الصحيح: الحركة الصغيرة تولد الرغبة، لا العكس.",
                },
                {
                    "id": "s3",
                    "narration": "خذ خطوة واحدة فقط الآن، مثل فتح الكتاب أو كتابة الجملة الأولى، ثم انظر كيف يتغير كل شيء.",
                },
            ]
        }
        report = {
            "status": "block",
            "unsupported_claims": [{"section_id": "s2", "issue": "The claim that small actions generate desire, contrary to the assumption that motivation precedes action."}],
            "diagnostics": {"raw_result": {"unsupported_claims": [{"section_id": "s2", "issue": "The claim that small actions generate desire, contrary to the assumption that motivation precedes action."}], "professional_advice_flags": [], "expert_persona_flags": []}},
            "professional_advice_flags": [],
            "expert_persona_flags": [],
            "notes": [
                "The script contains a psychological claim that action precedes motivation, which is not supported by the empty approved research context."
            ],
        }

        factuality_notes = _factuality_repair_issue_notes(report)
        location_notes = _factuality_location_issue_notes(report, script)
        revision_note = "\n".join(
            item for item in (factuality_notes, location_notes) if item
        )

        self.assertNotIn("s2", "\n".join(report["notes"] + [item["issue"] for item in report["unsupported_claims"]]))
        self.assertEqual(location_notes, "- [factuality-location] s2")
        self.assertEqual(
            _factuality_target_section_ids(report, script),
            ("s2",),
        )

    def test_factuality_provider_block_without_flags_is_local_pass(self):
        script = {
            "sections": [
                {"id": "s1", "narration": "هوك واضح. اللهم صلِّ وسلِّم على نبينا محمد. وهنا في نداء اليقظة، نقترب من أفكار الحياة اليومية بوعيٍ أوضح. ابدأ بخطوة صغيرة."},
                {"id": "s2", "narration": "اختر مهمة واحدة واكتبها الآن."},
                {"id": "s3", "narration": "راجع ما أنجزته في نهاية اليوم."},
            ]
        }
        plan = {"sections": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]}
        captured = {}

        def build_plan(*, brief, plan, script):
            captured["script"] = script
            return SimpleNamespace(sections=[])

        def audit(_key, _plan, _research, _model, *, diagnostics):
            diagnostics.update({"validation": "valid", "attempts": [], "raw_result": {"status": "block"}})
            return {
                "status": "block",
                "unsupported_claims": [],
                "professional_advice_flags": [],
                "expert_persona_flags": [],
                "notes": ["provider disagreed without a concrete hard flag"],
            }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "narrative-identity.json").write_text(
                json.dumps({"opener": "", "closer": "", "transitions": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch(
                "clean_v2.pipeline._build_production_plan_for_audit",
                side_effect=build_plan,
            ), patch(
                "clean_v2.text_audit.audit_plan_with_mistral",
                side_effect=audit,
            ):
                report = _run_legacy_factuality_audit(
                    output_dir=root,
                    brief={"format": "short", "research_pack": []},
                    plan=plan,
                    script=script,
                )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["provider_status"], "block")
        self.assertEqual(report["decision_source"], "deterministic_local_risk_policy")
        self.assertEqual(report["hard_flag_count"], 0)
        audited = "\n".join(item["narration"] for item in captured["script"]["sections"])
        self.assertNotIn("اللهم صلِّ وسلِّم على نبينا محمد.", audited)
        self.assertNotIn("وهنا في نداء اليقظة، نقترب من أفكار الحياة اليومية بوعيٍ أوضح.", audited)

    def test_factuality_local_flags_block_even_if_provider_status_pass(self):
        plan = {"sections": [{"id": "s1"}]}
        script = {"sections": [{"id": "s1", "narration": "هذه نسبة دقيقة غير موثقة."}]}

        def audit(_key, _plan, _research, _model, *, diagnostics):
            diagnostics.update({"validation": "valid", "attempts": [], "raw_result": {"status": "pass"}})
            return {
                "status": "pass",
                "unsupported_claims": [{"section_id": "s1", "issue": "unsupported statistic"}],
                "professional_advice_flags": [],
                "expert_persona_flags": [],
                "notes": [],
            }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch(
                "clean_v2.pipeline._build_production_plan_for_audit",
                return_value=SimpleNamespace(sections=[]),
            ), patch(
                "clean_v2.text_audit.audit_plan_with_mistral",
                side_effect=audit,
            ):
                with self.assertRaisesRegex(RuntimeError, "factuality/AI-expert gate blocked"):
                    _run_legacy_factuality_audit(
                        output_dir=root,
                        brief={"format": "short", "research_pack": []},
                        plan=plan,
                        script=script,
                    )
            persisted = json.loads((root / "factuality-audit.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], "block")
            self.assertEqual(persisted["provider_status"], "pass")
            self.assertEqual(persisted["hard_flag_count"], 1)

    def test_factuality_productivity_misflag_is_advisory_not_block(self):
        plan = {"sections": [{"id": "s1"}]}
        script = {"sections": [{"id": "s1", "narration": "اكتب مهمة واحدة وحدد وقتًا لمراجعتها."}]}

        def audit(_key, _plan, _research, _model, *, diagnostics):
            raw = {
                "status": "block",
                "unsupported_claims": [],
                "professional_advice_flags": [
                    {"section_id": "s1", "issue": "ordinary productivity guidance to write one task"}
                ],
                "expert_persona_flags": [],
                "notes": [],
            }
            diagnostics.update({"validation": "valid", "attempts": [], "raw_result": raw})
            return dict(raw)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch(
                "clean_v2.pipeline._build_production_plan_for_audit",
                return_value=SimpleNamespace(sections=[]),
            ), patch(
                "clean_v2.text_audit.audit_plan_with_mistral",
                side_effect=audit,
            ):
                report = _run_legacy_factuality_audit(
                    output_dir=root,
                    brief={"format": "short", "research_pack": []},
                    plan=plan,
                    script=script,
                )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["provider_status"], "block")
        self.assertEqual(report["hard_flag_count"], 0)
        self.assertEqual(len(report["advisory_flags"]["professional_advice_flags"]), 1)

    def test_factuality_medical_advice_flag_is_hard_block(self):
        plan = {"sections": [{"id": "s1"}]}
        script = {"sections": [{"id": "s1", "narration": "غيّر جرعة الدواء لعلاج الأعراض."}]}

        def audit(_key, _plan, _research, _model, *, diagnostics):
            raw = {
                "status": "pass",
                "unsupported_claims": [],
                "professional_advice_flags": [
                    {"section_id": "s1", "issue": "medical treatment advice"}
                ],
                "expert_persona_flags": [],
                "notes": [],
            }
            diagnostics.update({"validation": "valid", "attempts": [], "raw_result": raw})
            return dict(raw)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch(
                "clean_v2.pipeline._build_production_plan_for_audit",
                return_value=SimpleNamespace(sections=[]),
            ), patch(
                "clean_v2.text_audit.audit_plan_with_mistral",
                side_effect=audit,
            ):
                with self.assertRaisesRegex(RuntimeError, "factuality/AI-expert gate blocked"):
                    _run_legacy_factuality_audit(
                        output_dir=root,
                        brief={"format": "short", "research_pack": []},
                        plan=plan,
                        script=script,
                    )
            persisted = json.loads((root / "factuality-audit.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], "block")
            self.assertEqual(persisted["hard_flag_count"], 1)

    def test_valid_content_block_is_quality_block_not_infrastructure(self):
        blocked = _tone_result(status="block")
        blocked["naturalness_flags"] = ["generic AI filler"]
        dummy_plan = SimpleNamespace(hook="")

        with tempfile.TemporaryDirectory() as tmp, patch(
            "clean_v2.pipeline._build_production_plan_for_audit",
            return_value=dummy_plan,
        ), patch(
            "clean_v2.tone_audit.audit_tone_and_naturalness_with_mistral",
            return_value=blocked,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "tone/naturalness gate blocked real production",
            ):
                _run_legacy_tone_naturalness_audit(
                    output_dir=Path(tmp),
                    brief={"format": "film"},
                    plan={"sections": []},
                    script={
                        "sections": [
                            {"id": "s1", "narration": "افتتاح واضح. ثم شرح طبيعي."}
                        ]
                    },
                )
            persisted = json.loads(
                (Path(tmp) / "tone-naturalness-audit.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(persisted["status"], "block")
            self.assertEqual(dummy_plan.hook, "افتتاح واضح.")

    def test_provider_exhaustion_stays_infrastructure(self):
        exhausted = _tone_result(status="block", validation="providers_exhausted")
        exhausted["attempts"] = [
            {"provider": "gemini", "outcome": "rate_limited"},
            {"provider": "groq", "outcome": "rate_limited"},
            {"provider": "openrouter", "outcome": "other"},
            {"provider": "mistral", "outcome": "rate_limited"},
        ]

        with tempfile.TemporaryDirectory() as tmp, patch(
            "clean_v2.pipeline._build_production_plan_for_audit",
            return_value=SimpleNamespace(hook=""),
        ), patch(
            "clean_v2.tone_audit.audit_tone_and_naturalness_with_mistral",
            return_value=exhausted,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "text_audit exhausted bounded provider route: tone_naturalness",
            ):
                _run_legacy_tone_naturalness_audit(
                    output_dir=Path(tmp),
                    brief={"format": "film"},
                    plan={"sections": []},
                    script={"sections": [{"id": "s1", "narration": "افتتاح واضح."}]},
                )

    def test_run15_no_effect_tone_repair_fails_closed_before_reaudit(self):
        script = {
            "sections": [
                {"id": "s1", "narration": "أجلس وأنتظر الدافع."},
                {"id": "s2", "narration": "أقول لنفسي إن الحركة مؤجلة."},
                {"id": "s3", "narration": "أفتح الدفتر وأكتب كلمة واحدة."},
            ]
        }
        original = json.loads(json.dumps(script, ensure_ascii=False))

        class NoOpRouter:
            def route(self, **_kwargs):
                return json.loads(json.dumps(original, ensure_ascii=False))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "narrative-identity.json").write_text(
                json.dumps({"opener": "", "closer": "", "transitions": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            (root / "cta-plan.json").write_text(
                json.dumps({"mode": "none", "spoken_text": ""}, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch(
                "clean_v2.pipeline._tone_repair_issue_notes",
                return_value="- [tone] inner dialogue still sounds external",
            ), patch(
                "clean_v2.pipeline._structural_repair_issue_notes",
                return_value="",
            ), patch(
                "clean_v2.pipeline._short_template_tone_repair_issue_notes",
                return_value="",
            ), patch(
                "clean_v2.pipeline._repair_target_section_ids",
                return_value=("s1", "s2", "s3"),
            ):
                with self.assertRaisesRegex(RuntimeError, "TONE_REPAIR_NO_EFFECT"):
                    _run_one_bounded_tone_repair(
                        output_dir=root,
                        brief={"format": "short"},
                        plan={"sections": []},
                        script=script,
                        router=NoOpRouter(),
                        blocked_report={"status": "block"},
                    )

            self.assertEqual(script, original)
            candidate = json.loads(
                (root / "script-post-tone-repair.json").read_text(encoding="utf-8")
            )
            self.assertEqual(candidate, original)
            report = json.loads(
                (root / "tone-repair.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["status"], "failed_closed")
            self.assertEqual(report["reason"], "TONE_REPAIR_NO_EFFECT")
            self.assertFalse(report["narration_changed"])

    def test_tone_repair_guard_allows_real_narration_change(self):
        script = {
            "sections": [
                {"id": "s1", "narration": "أجلس وأنتظر الدافع."},
                {"id": "s2", "narration": "أقول لنفسي إن الحركة مؤجلة."},
                {"id": "s3", "narration": "أفتح الدفتر وأكتب كلمة واحدة."},
            ]
        }
        repaired = json.loads(json.dumps(script, ensure_ascii=False))
        repaired["sections"][1]["narration"] = "لاحظت أنني أؤجل الحركة وأنا أنتظر شعورًا قد لا يأتي."

        class ChangedRouter:
            def route(self, **_kwargs):
                return json.loads(json.dumps(repaired, ensure_ascii=False))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "narrative-identity.json").write_text(
                json.dumps({"opener": "", "closer": "", "transitions": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            (root / "cta-plan.json").write_text(
                json.dumps({"mode": "none", "spoken_text": ""}, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch(
                "clean_v2.pipeline._tone_repair_issue_notes",
                return_value="- [tone] inner dialogue still sounds external",
            ), patch(
                "clean_v2.pipeline._structural_repair_issue_notes",
                return_value="",
            ), patch(
                "clean_v2.pipeline._short_template_tone_repair_issue_notes",
                return_value="",
            ), patch(
                "clean_v2.pipeline._repair_target_section_ids",
                return_value=("s2",),
            ), patch(
                "clean_v2.pipeline._assert_brand_signature_invariant",
                return_value=None,
            ):
                report = _run_one_bounded_tone_repair(
                    output_dir=root,
                    brief={"format": "short"},
                    plan={"sections": []},
                    script=script,
                    router=ChangedRouter(),
                    blocked_report={"status": "block"},
                )

            self.assertTrue(report["narration_changed"])
            self.assertEqual(script["sections"][1]["narration"], repaired["sections"][1]["narration"])

    def test_composite_runs_factuality_before_tone(self):
        order = []

        def factuality(**kwargs):
            order.append("factuality")
            return {"status": "pass"}

        def tone(**kwargs):
            order.append("tone")
            return {"status": "pass"}

        with patch(
            "clean_v2.pipeline._run_legacy_factuality_audit",
            side_effect=factuality,
        ), patch(
            "clean_v2.pipeline._run_legacy_tone_naturalness_audit",
            side_effect=tone,
        ):
            report = _run_text_audits(
                output_dir=Path("."),
                brief={},
                plan={},
                script={},
            )

        self.assertEqual(order, ["factuality", "tone"])
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["factuality_status"], "pass")
        self.assertEqual(report["tone_naturalness_status"], "pass")


if __name__ == "__main__":
    unittest.main()
