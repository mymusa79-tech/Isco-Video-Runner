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
    _first_spoken_sentence,
    _repair_target_section_ids,
    _run_legacy_tone_naturalness_audit,
    _run_text_audits,
)
from clean_v2.tone_audit import (
    TONE_AUDIT_SCHEMA,
    _mistral_tone_call,
    _scope_clean_v2_tone_prompt,
    _scope_religious_quote_prompt,
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
        self.assertEqual(name, "clean_v2_tone_naturalness_audit_v1")
        self.assertIs(schema, TONE_AUDIT_SCHEMA)

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
            "unsupported_claims": [
                "claim that expectations influence emotions more than energy"
            ],
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
            _repair_target_section_ids(script, revision_note, {}),
            ("s2",),
        )

    def test_short_cohort_attempt_1_without_section_id_resolves_s2_from_claim_content(self):
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
            "unsupported_claims": [
                "The claim that small actions generate desire, contrary to the assumption that motivation precedes action."
            ],
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

        self.assertNotIn("s2", "\n".join(report["notes"] + report["unsupported_claims"]))
        self.assertEqual(location_notes, "- [factuality-location] s2")
        self.assertEqual(
            _repair_target_section_ids(script, revision_note, {}),
            ("s2",),
        )

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
