from __future__ import annotations

import unittest
from unittest import mock

from clean_v2 import providers
from clean_v2.pipeline import (\n    _short_locked_action_repair_allowed,\n    _tone_repair_issue_notes,\n    _repair_target_section_ids,\n    _required_semantic_repair_section_ids,\n)\nfrom clean_v2.short_format import (
    ShortFormatError,
    normalize_short_practical_action,
    validate_short_practical_action_specificity,
)
from clean_v2.tone_audit import (
    _LEGACY_RELIGIOUS_QUOTE_RULE,
    _scope_clean_v2_tone_prompt,
)


class Run99ShortActionTests(unittest.TestCase):
    def test_terminal_decorative_dash_is_removed_before_lock(self):
        self.assertEqual(
            normalize_short_practical_action(
                "اكتب اليوم سببًا واحدًا يوضح متى تصبح الراحة هروبًا –"
            ),
            "اكتب اليوم سببًا واحدًا يوضح متى تصبح الراحة هروبًا",
        )

    def test_generic_things_bucket_is_rejected_before_planning_lock(self):
        with self.assertRaisesRegex(
            ShortFormatError, "short_practical_action_generic_placeholder"
        ):
            validate_short_practical_action_specificity(
                "اكتب اليوم ثلاث أشياء تجنبها تحت عنوان هروبي.",
                topic_context="متى تتحول الراحة إلى هروب؟",
            )

    def test_topic_bound_action_remains_valid(self):
        action = validate_short_practical_action_specificity(
            "اكتب موقفًا واحدًا تتحول فيه الراحة إلى هروب.",
            topic_context="متى تتحول الراحة إلى هروب؟",
        )
        self.assertIn("الراحة", action)
        self.assertIn("هروب", action)

    def test_generic_standin_needs_topic_anchor(self):
        with self.assertRaisesRegex(
            ShortFormatError, "short_practical_action_too_generic_for_topic"
        ):
            validate_short_practical_action_specificity(
                "اختر خطوة واحدة واضحة اليوم.",
                topic_context="متى تتحول الراحة إلى هروب؟",
            )


class Run99LockedActionRepairTests(unittest.TestCase):
    def setUp(self):
        self.plan = {
            "practical_action_ar": "اكتب اليوم ثلاث أشياء تجنبها تحت عنوان هروبي.",
            "s3_locked_action": "اكتب اليوم ثلاث أشياء تجنبها تحت عنوان هروبي.",
        }

    def test_stable_generic_action_audit_marker_opens_only_the_action(self):
        note = (
            "- [tone] content_depth:s3 practical_action_generic: "
            "الخطوة عامة ولا تشغّل التوتر المحدد في الهوك."
        )
        self.assertTrue(_short_locked_action_repair_allowed(self.plan, note))

    def test_untyped_content_depth_objection_does_not_unlock_host_action(self):
        note = "- [tone] content_depth:s3 الخاتمة تحتاج عمقًا أكبر."
        self.assertFalse(_short_locked_action_repair_allowed(self.plan, note))

    def test_run181_first_audit_inventory_covers_body_and_action_in_one_repair(self):
        report = {
            "naturalness_flags": ["s3: dangling conditional payoff fragment"],
            "narrative_format_flags": [
                "content_depth:s2 body restates the exact hook without explaining it",
                "content_depth:s3 practical_action_generic: closing action could fit unrelated topics",
                "content_dependency:s3 payoff does not repay the exact opening tension",
            ],
        }
        script = {
            "sections": [
                {"id": "s1", "narration": "تريد التحرك لكنك تبقى في مكانك."},
                {"id": "s2", "narration": "هذا الجمود يعود إلى توقع بداية مثالية."},
                {"id": "s3", "narration": "عندما تفكر في الإنجاز. اكتب جملة واحدة فقط اليوم."},
            ]
        }
        notes = _tone_repair_issue_notes(report, script)
        self.assertIn("content_depth:s2", notes)
        self.assertIn("content_depth:s3 practical_action_generic:", notes)
        self.assertIn("s3: dangling conditional payoff", notes)
        self.assertEqual(_repair_target_section_ids(script, notes, {}), ("s2", "s3"))
        self.assertEqual(
            _required_semantic_repair_section_ids(script, notes), ("s2", "s3")
        )
        self.assertTrue(_short_locked_action_repair_allowed(self.plan, notes))

    def test_run181_audit_prompt_reports_all_real_defects_without_extra_call(self):
        scoped = _scope_clean_v2_tone_prompt(_LEGACY_RELIGIOUS_QUOTE_RULE)
        self.assertIn("ONE-PASS COMPLETE DEFECT INVENTORY", scoped)
        self.assertIn("Do not stop after", scoped)
        self.assertIn("content_depth:s2", scoped)
        self.assertIn("content_depth:s3", scoped)
        self.assertIn("practical_action_generic:", scoped)
        self.assertIn("Do not add a second audit or provider call", scoped)
        self.assertIn("do not call it a dangling", scoped)

    def test_tone_prompt_requires_stable_generic_action_marker(self):
        scoped = _scope_clean_v2_tone_prompt(_LEGACY_RELIGIOUS_QUOTE_RULE)
        self.assertIn("content_depth:s3 practical_action_generic:", scoped)


class Run99ProviderRecoveryTests(unittest.TestCase):
    def test_openrouter_schema_call_does_not_require_parameter_capability(self):
        seen = {}

        def fake_post(_url, *, headers, payload, timeout):
            seen.update(payload)
            return {
                "choices": [
                    {"message": {"content": '{"status":"pass"}'}}
                ]
            }

        with mock.patch.object(providers, "_read_secret", return_value="key"), \
             mock.patch.object(providers, "_post_json", side_effect=fake_post):
            result = providers._openrouter_call(
                "audit",
                100,
                response_schema={
                    "type": "object",
                    "properties": {"status": {"type": "string"}},
                    "required": ["status"],
                    "additionalProperties": False,
                },
            )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(seen["provider"], {"allow_fallbacks": True})
        self.assertNotIn("require_parameters", seen["provider"])

    def test_groq_strict_schema_400_retries_once_as_json_object(self):
        payloads = []
        first = providers.ProviderWireFailure(
            "http_400",
            http_status=400,
            error_detail="response_format json_schema is not supported for this request",
        )

        def fake_post(_url, *, headers, payload, timeout):
            payloads.append(payload)
            if len(payloads) == 1:
                raise first
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"status":"pass"}'},
                    }
                ]
            }

        with mock.patch.object(providers, "_read_secret", return_value="key"), \
             mock.patch.object(providers, "_post_json", side_effect=fake_post):
            result = providers._groq_call(
                "audit",
                100,
                response_schema={
                    "type": "object",
                    "properties": {"status": {"type": "string"}},
                    "required": ["status"],
                    "additionalProperties": False,
                },
                schema_name="run99_audit",
            )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0]["response_format"]["type"], "json_schema")
        self.assertEqual(payloads[1]["response_format"], {"type": "json_object"})

    def test_groq_capacity_400_is_not_retried_and_body_is_diagnostic(self):
        calls = []
        failure = providers.ProviderWireFailure(
            "http_400",
            http_status=400,
            error_detail="request too large for model token limit",
        )

        def fake_post(*args, **kwargs):
            calls.append(1)
            raise failure

        with mock.patch.object(providers, "_read_secret", return_value="key"), \
             mock.patch.object(providers, "_post_json", side_effect=fake_post):
            with self.assertRaises(providers.ProviderWireFailure) as ctx:
                providers._groq_call(
                    "audit",
                    100,
                    response_schema={"type": "object"},
                )

        self.assertEqual(len(calls), 1)
        self.assertEqual(ctx.exception.reason_code, "http_400")
        self.assertIn("request too large", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
