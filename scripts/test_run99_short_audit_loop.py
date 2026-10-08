from __future__ import annotations

import unittest
from unittest import mock

from clean_v2 import providers
from clean_v2.pipeline import _short_locked_action_repair_allowed
from clean_v2.short_format import (
    ShortFormatError,
    normalize_short_practical_action,
    validate_short_practical_action_specificity,
    validate_short_s3_contract,
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

    def test_run102_descriptive_choice_noun_is_not_hidden_action(self):
        report = validate_short_s3_contract(
            "حين يهدأ البحث عن المثالي، يصبح الاختيار أسهل وأوضح.",
            "حدد معيارًا واحدًا يكفيك قبل مقارنة الخيارات.",
        )
        self.assertIn("الاختيار", report["s3_payoff"])

    def test_run102_choice_imperative_remains_forbidden_in_payoff(self):
        with self.assertRaisesRegex(
            ShortFormatError, "short_s3_payoff_contains_forbidden_action_family"
        ):
            validate_short_s3_contract(
                "اختر ما يكفيك ثم تجاهل الباقي.",
                "حدد معيارًا واحدًا يكفيك قبل مقارنة الخيارات.",
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
