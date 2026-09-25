from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest import mock

from clean_v2.contracts import (
    ContractError,
    compute_brief_sha256,
    load_approved_brief,
    validate_narrative_identity,
    validate_plan,
    validate_script,
)
from clean_v2.pipeline import (
    AUDIO_MASTERING_STAGE,
    CINEMATIC_STAGE,
    IDENTITY_STAGE,
    OPENING_STAGE,
    TEXT_AUDIT_STAGE,
    VISUAL_QA_STAGE,
    STAGES,
    CleanV2Pipeline,
    CleanV2FactualityContentBlock,
    CleanV2ToneContentBlock,
    _audit_narrative_format_for_brief,
    _factuality_repair_prompt,
    _factuality_target_section_ids,
    _repair_target_section_ids,
    _run_legacy_tone_naturalness_audit,
    _run_text_audits,
    _run_text_audit_with_one_bounded_tone_repair,
    _script_text_haystack,
    _tone_repair_issue_notes,
    _tone_repair_prompt,
    _validate_and_apply_script_patches,
    _validate_tone_repair_script,
    _PLANNING_FACTUALITY_RULE,
    _narrative_identity_prompt,
    _planning_prompt,
    _script_prompt,
)
from clean_v2.providers import (
    NoWireFailure,
    ProviderAdapter,
    ProviderRouter,
    ProviderWireFailure,
)
from clean_v2.identity_sequence import PRAYER_SENTENCE
from clean_v2.short_format import select_short_template
from clean_v2 import visual_qa as visual_qa_module
from clean_v2 import providers as providers_module
from clean_v2 import media as media_module
from clean_v2 import text_audit as text_audit_module


def _brief() -> dict:
    return {
        "approved_by_user": True,
        "approved_topic": "كيف تبدأ بخطوة صغيرة",
        "format": "film",
        "language": "ar",
        "audience": "Arabic-speaking adults",
        "editorial_intent": "شرح عملي هادئ دون وعود مبالغ فيها.",
        "research_pack": [],
        "hard_constraints": ["No fabricated facts."],
    }


def _plan() -> dict:
    return {
        "title": "خطوة واحدة",
        "promise": "فهم طريقة عملية للبدء",
        "cta": "إذا كانت هذه الفكرة قريبة منك، اكتب تجربتك في التعليقات.",
        "sections": [
            {
                "id": "s1",
                "heading": "المشكلة",
                "purpose": "تسمية العائق",
                "visual_query_en": "quiet desk notebook wide shot",
            },
            {
                "id": "s2",
                "heading": "الفكرة",
                "purpose": "شرح الخطوة الصغيرة",
                "visual_query_en": "hand writing one task in notebook",
            },
            {
                "id": "s3",
                "heading": "التطبيق",
                "purpose": "دعوة عملية",
                "visual_query_en": "morning workspace sunlight no face",
            },
            {
                "id": "s4",
                "heading": "المراجعة",
                "purpose": "مراجعة أثر الخطوة الأولى",
                "visual_query_en": "checking simple task list on desk",
            },
            {
                "id": "s5",
                "heading": "الاستمرار",
                "purpose": "تثبيت خطوة تالية واضحة",
                "visual_query_en": "calendar and notebook calm workspace",
            },
        ],
    }


def _script() -> dict:
    return {
        "title": "خطوة واحدة",
        "sections": [
            {
                "id": "s1",
                "narration": "نؤجل البداية أحيانًا لأن المهمة تبدو أكبر من اللحظة المتاحة أمامنا.",
            },
            {
                "id": "s2",
                "narration": "حين نصغر الفعل الأول يصبح البدء أوضح، ونختبر الواقع بدل أن نبقى داخل الخطة.",
            },
            {
                "id": "s3",
                "narration": "اختر اليوم خطوة يمكن تنفيذها الآن، ثم اترك النتيجة التالية لما بعد البداية.",
            },
            {
                "id": "s4",
                "narration": "بعد التنفيذ راجع ما حدث بهدوء، وما الذي جعل الخطوة ممكنة في هذه المرة.",
            },
            {
                "id": "s5",
                "narration": "ثبت ما نجح واختر خطوة تالية صغيرة وواضحة حتى يتحول التقدم إلى عادة عملية.",
            },
        ],
    }


class ApprovedBriefContractTests(unittest.TestCase):
    def test_hash_binding_accepts_exact_brief_and_rejects_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "brief.json"
            brief = _brief()
            path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            digest = compute_brief_sha256(brief)
            self.assertEqual(load_approved_brief(path, digest)["approved_topic"], brief["approved_topic"])

            brief["approved_topic"] = "موضوع مختلف"
            path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "changed after approval"):
                load_approved_brief(path, digest)

    def test_public_clean_v2_contract_rejects_legacy_partial_formats(self) -> None:
        for fmt in ("moment", "story"):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "brief.json"
                brief = _brief()
                brief["format"] = fmt
                path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
                digest = compute_brief_sha256(brief)
                with self.assertRaisesRegex(ContractError, "unsupported approved format"):
                    load_approved_brief(path, digest)


class PlanningCardinalityTests(unittest.TestCase):
    def test_film_prompt_and_validator_require_exactly_five_sections(self) -> None:
        brief = _brief()
        brief["format"] = "film"
        prompt = _planning_prompt(brief)
        self.assertIn("Use exactly 5 sections", prompt)
        self.assertNotIn("5 to 6", prompt)
        self.assertEqual(len(validate_plan(_plan(), brief)["sections"]), 5)
        normalized = validate_plan(_plan(), brief)
        self.assertIn("cta", normalized)
        self.assertIn("exactly ONE natural primary action", prompt)

        six = _plan()
        six["sections"].append(
            {
                "id": "s6",
                "heading": "سادس",
                "purpose": "يجب رفضه",
                "visual_query_en": "extra calm workspace shot",
            }
        )
        with self.assertRaisesRegex(
            ContractError,
            "exactly 5 for film",
        ):
            validate_plan(six, brief)

    def test_planning_keeps_stock_queries_in_one_lighting_world(self) -> None:
        prompt = _planning_prompt(_brief())
        self.assertIn("warm natural morning/daylight", prompt)
        self.assertIn("neutral-warm tones", prompt)
        self.assertIn("do not mix obvious neon/night/cold-blue looks", prompt)
        self.assertIn("foreground/midground/background depth", prompt)
        self.assertIn("practical light sources", prompt)
        self.assertIn("avoid empty walls, flat generic desks", prompt)
        self.assertIn("left or lower-left", prompt)
        self.assertIn("upper-right for", prompt)

    def test_run249_planning_keeps_complete_visual_query_and_rejects_truncation_shapes(self) -> None:
        brief = _brief()
        brief["format"] = "film"

        plan = _plan()
        long_but_valid_query = "desk calendar task list " + ("detail " * 28)
        self.assertGreater(len(long_but_valid_query), 200)
        self.assertLessEqual(len(long_but_valid_query), 260)
        plan["sections"][2]["visual_query_en"] = long_but_valid_query
        normalized = validate_plan(plan, brief)
        self.assertEqual(
            normalized["sections"][2]["visual_query_en"],
            long_but_valid_query.strip(),
        )

        too_long = _plan()
        too_long["sections"][2]["visual_query_en"] = "x" * 261
        with self.assertRaisesRegex(
            ContractError,
            "visual_query_en exceeds 260 characters",
        ):
            validate_plan(too_long, brief)

        cut_purpose = _plan()
        cut_purpose["sections"][2]["purpose"] = "يوضح الفرق بين النية العامة ("
        with self.assertRaisesRegex(ContractError, "purpose looks truncated"):
            validate_plan(cut_purpose, brief)

        prompt = _planning_prompt(brief)
        self.assertIn("at most 260 characters", prompt)
        self.assertIn("never cut mid-thought", prompt)


class TextAuditProfessionalAdviceScopeTests(unittest.TestCase):
    def test_attempt2_productivity_guidance_is_explicitly_outside_professional_advice_flag(self) -> None:
        legacy_prompt = (
            "Rules:\n"
            + text_audit_module._LEGACY_PROFESSIONAL_ADVICE_RULE
            + "\n<PLAN>اختر مهمة واحدة، اضبط تذكيرًا، وتابع تقدمك.</PLAN>"
        )
        scoped = text_audit_module._scope_professional_advice_prompt(legacy_prompt)

        self.assertIn("choose a task, set a reminder, or track progress", scoped)
        self.assertIn("do NOT flag ordinary general productivity/self-improvement advice", scoped)
        self.assertIn("individualized medical, legal, financial", scoped)
        self.assertIn("religious authority advice/claims", scoped)
        self.assertIn("diagnosis, treatment, prescriptions", scoped)
        self.assertEqual(scoped.count("For professional_advice_flags specifically:"), 1)

    def test_professional_advice_scope_fails_closed_if_legacy_rule_drifts(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "professional-advice rule drift"):
            text_audit_module._scope_professional_advice_prompt(
                "Rules changed unexpectedly; no legacy professional advice rule here."
            )


class ScriptPromptFactualityRuleTests(unittest.TestCase):
    def test_script_prompt_contains_exact_legacy_factuality_rule_and_research_pack(self) -> None:
        brief = _brief()
        brief["research_pack"] = [
            {
                "source_title": "Controlled source",
                "source_url": "https://example.com/source",
                "claim_scope": "General relationship only; no invented numbers or stronger causation.",
            }
        ]
        prompt = _script_prompt(brief, _plan())

        self.assertIn(_PLANNING_FACTUALITY_RULE, prompt)
        self.assertEqual(prompt.count(_PLANNING_FACTUALITY_RULE), 1)
        self.assertIn('"research_pack"', prompt)
        self.assertIn('"claim_scope"', prompt)

    def test_script_prompt_authors_strong_hook_before_tone_audit(self) -> None:
        prompt = _script_prompt(_brief(), _plan())
        normalized_prompt = " ".join(prompt.split())
        self.assertIn("strongest natural entry into THIS exact episode", normalized_prompt)
        self.assertIn("specific situation, tension, behavior, consequence, or", normalized_prompt)
        self.assertIn("forced shock/clickbait", normalized_prompt)
        self.assertIn("same core tension the script will develop", normalized_prompt.lower())
        self.assertIn("do not optimize for a fixed word count or duration", normalized_prompt.lower())

    def test_long_script_prompt_receives_exact_identity_handoff_for_smooth_topic_entry(self) -> None:
        opener = "هذه نداء اليقظة، مساحة للوعي الصادق والنهوض الهادئ نحو حياة أوضح."
        prompt = _script_prompt(_brief(), _plan(), identity_opener=opener)

        self.assertIn("اللهم صلِّ وسلِّم على نبينا محمد.", prompt)
        self.assertIn(opener, prompt)
        self.assertIn("one continuous thought, not three separate announcements", prompt)
        self.assertIn("semantic bridge", prompt)

    def test_actual_script_router_sends_same_factuality_rule_to_all_four_providers(self) -> None:
        prompt = _script_prompt(_brief(), _plan())
        captured: dict[str, str] = {}

        def failing_call(name):
            def call(actual_prompt, _max_tokens, **_kwargs):
                captured[name] = actual_prompt
                raise ProviderWireFailure("http_503", http_status=503)
            return call

        def mistral_call(actual_prompt, _max_tokens, stage):
            if stage != "script":
                raise AssertionError(stage)
            captured["mistral"] = actual_prompt
            return {"ok": True}

        with (
            mock.patch.object(providers_module, "_gemini_call", side_effect=failing_call("gemini")),
            mock.patch.object(providers_module, "_groq_call", side_effect=failing_call("groq")),
            mock.patch.object(providers_module, "_openrouter_call", side_effect=failing_call("openrouter")),
            mock.patch.object(providers_module, "_mistral_call", side_effect=mistral_call),
        ):
            router = ProviderRouter(providers_module.default_adapters())
            result = router.route(
                stage="script",
                prompt=prompt,
                max_tokens=7500,
                validator=lambda value: value,
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            list(captured),
            ["gemini", "groq", "openrouter", "mistral"],
        )
        for provider in ("gemini", "groq", "openrouter", "mistral"):
            self.assertEqual(captured[provider], prompt)
            self.assertIn(_PLANNING_FACTUALITY_RULE, captured[provider])


class ProviderCapacityMemoryTests(unittest.TestCase):
    def test_long_or_unspecified_429_is_skipped_for_remaining_run_stages(self) -> None:
        calls = {"limited": 0, "fallback": 0}

        def limited(_prompt, _tokens):
            calls["limited"] += 1
            raise ProviderWireFailure(
                "http_429",
                http_status=429,
                retry_after_seconds=None,
            )

        def fallback(_prompt, _tokens):
            calls["fallback"] += 1
            return {"ok": True}

        router = ProviderRouter(
            (
                ProviderAdapter("limited", limited),
                ProviderAdapter("fallback", fallback),
            )
        )
        self.assertEqual(
            router.route(
                stage="planning",
                prompt="planning",
                max_tokens=100,
                validator=lambda value: value,
            ),
            {"ok": True},
        )
        self.assertEqual(
            router.route(
                stage="script",
                prompt="script",
                max_tokens=100,
                validator=lambda value: value,
            ),
            {"ok": True},
        )

        self.assertEqual(calls, {"limited": 1, "fallback": 2})
        cached = [
            event
            for event in router.events
            if event["provider"] == "limited"
            and event["reason"] == "rate_limit_cached"
        ]
        self.assertEqual(len(cached), 1)
        self.assertFalse(cached[0]["wire_attempted"])


class GroqJsonModeContractTests(unittest.TestCase):
    def test_gptoss_json_mode_hides_reasoning(self) -> None:
        captured: dict[str, object] = {}

        def post_json(url, *, headers, payload, timeout):
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = payload
            captured["timeout"] = timeout
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"ok": true}'},
                    }
                ]
            }

        with (
            mock.patch.object(providers_module, "_read_secret", return_value="secret"),
            mock.patch.object(providers_module, "_post_json", side_effect=post_json),
            mock.patch.dict(
                os.environ,
                {"GROQ_CONTENT_MODEL": "openai/gpt-oss-20b"},
                clear=False,
            ),
        ):
            result = providers_module._groq_call("prompt", 321)

        self.assertEqual(result, {"ok": True})
        payload = captured["payload"]
        self.assertEqual(payload["model"], "openai/gpt-oss-20b")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertIs(payload["include_reasoning"], False)
        self.assertNotIn("reasoning_format", payload)
        self.assertEqual(payload["max_completion_tokens"], 321)


    def test_planning_uses_groq_strict_structured_output(self) -> None:
        captured: dict[str, object] = {}

        def post_json(url, *, headers, payload, timeout):
            del url, headers, timeout
            captured["payload"] = payload
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"ok": true}'},
                    }
                ]
            }

        with (
            mock.patch.object(providers_module, "_read_secret", return_value="secret"),
            mock.patch.object(providers_module, "_post_json", side_effect=post_json),
        ):
            providers_module._groq_stage_call(
                _planning_prompt(_brief()),
                800,
                "planning",
            )

        response_format = captured["payload"]["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])
        schema = response_format["json_schema"]["schema"]
        section = schema["properties"]["sections"]["items"]
        self.assertIn("id", section["required"])
        self.assertEqual(
            set(section["required"]),
            set(section["properties"]),
        )

    def test_script_uses_groq_strict_schema_without_prefix_items(self) -> None:
        captured: dict[str, object] = {}

        def post_json(url, *, headers, payload, timeout):
            del url, headers, timeout
            captured["payload"] = payload
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"ok": true}'},
                    }
                ]
            }

        with (
            mock.patch.object(providers_module, "_read_secret", return_value="secret"),
            mock.patch.object(providers_module, "_post_json", side_effect=post_json),
        ):
            providers_module._groq_stage_call(
                _script_prompt(_brief(), _plan()),
                1200,
                "script",
            )

        response_format = captured["payload"]["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])
        sections = response_format["json_schema"]["schema"]["properties"]["sections"]
        self.assertNotIn("prefixItems", sections)
        self.assertEqual(sections["minItems"], len(_plan()["sections"]))
        self.assertEqual(sections["maxItems"], len(_plan()["sections"]))



class MistralPlanningSchemaTests(unittest.TestCase):
    @staticmethod
    def _brief_for(fmt: str) -> dict:
        brief = _brief()
        brief["format"] = fmt
        return brief

    @staticmethod
    def _plan_with_count(count: int) -> dict:
        return {
            "title": "خطة",
            "promise": "وعد واضح",
            "cta": "اكتب رأيك في التعليقات.",
            "sections": [
                {
                    "heading": f"قسم {index}",
                    "purpose": f"غرض {index}",
                    "visual_query_en": f"hands writing task {index} on notebook",
                }
                for index in range(1, count + 1)
            ],
        }

    def test_film_schema_requires_exactly_five_sections(self) -> None:
        schema = providers_module._mistral_planning_response_schema(
            _planning_prompt(self._brief_for("film"))
        )
        sections = schema["properties"]["sections"]
        self.assertEqual(sections["minItems"], 5)
        self.assertEqual(sections["maxItems"], 5)
        self.assertIn("cta", schema["required"])
        self.assertEqual(schema["properties"]["cta"]["minLength"], 1)
        self.assertEqual(schema["properties"]["cta"]["pattern"], r"\S")

        item = sections["items"]
        self.assertEqual(
            item["required"],
            ["heading", "purpose", "visual_query_en"],
        )
        self.assertNotIn("id", item["required"])
        self.assertNotIn("maxLength", item["properties"]["heading"])
        self.assertFalse(item["additionalProperties"])

    def test_short_schema_matches_validator_one_to_five_not_prompt_two_to_four(self) -> None:
        for fmt in ("moment", "story"):
            with self.subTest(fmt=fmt):
                brief = self._brief_for(fmt)
                prompt = _planning_prompt(brief)
                self.assertIn("2 to 4 sections", prompt)

                schema = providers_module._mistral_planning_response_schema(prompt)
                sections = schema["properties"]["sections"]
                self.assertEqual(sections["minItems"], 1)
                self.assertEqual(sections["maxItems"], 5)

                one = validate_plan(self._plan_with_count(1), brief)
                five = validate_plan(self._plan_with_count(5), brief)
                self.assertEqual(len(one["sections"]), 1)
                self.assertEqual(len(five["sections"]), 5)
                self.assertEqual(one["sections"][0]["id"], "s1")
                self.assertEqual(five["sections"][-1]["id"], "s5")

    def test_mistral_planning_call_passes_format_aware_strict_schema(self) -> None:
        prompt = _planning_prompt(self._brief_for("film"))
        expected = _plan()

        with mock.patch.object(
            providers_module.mistral_executor,
            "mistral_executor_json",
            return_value=expected,
        ) as called:
            result = providers_module._mistral_call(prompt, 4000, "planning")

        self.assertEqual(result, expected)
        kwargs = called.call_args.kwargs
        self.assertEqual(kwargs["task_kind"], "planning")
        self.assertEqual(kwargs["max_tokens"], 4000)
        self.assertEqual(kwargs["temperature"], 0.0)
        name, schema = kwargs["response_schema"]
        self.assertEqual(name, "planning")
        self.assertEqual(schema["properties"]["sections"]["minItems"], 5)
        self.assertEqual(schema["properties"]["sections"]["maxItems"], 5)

    def test_planning_schema_context_failure_is_no_wire(self) -> None:
        with mock.patch.object(
            providers_module.mistral_executor,
            "mistral_executor_json",
        ) as called:
            with self.assertRaisesRegex(
                NoWireFailure,
                "mistral_planning_approved_brief_missing",
            ):
                providers_module._mistral_call(
                    "planning prompt without approved brief",
                    4000,
                    "planning",
                )
        called.assert_not_called()


class MistralNarrativeIdentitySchemaTests(unittest.TestCase):
    def test_schema_matches_validator_shape_without_artificial_length_caps(self) -> None:
        schema = providers_module.MISTRAL_NARRATIVE_IDENTITY_SCHEMA

        self.assertEqual(schema["required"], ["opener", "closer", "transitions"])
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("maxLength", schema["properties"]["opener"])
        self.assertNotIn("maxLength", schema["properties"]["closer"])

        transitions = schema["properties"]["transitions"]
        self.assertEqual(transitions["minItems"], 3)
        self.assertEqual(transitions["maxItems"], 3)
        self.assertEqual(len(transitions["prefixItems"]), 3)
        for item in transitions["prefixItems"]:
            self.assertEqual(item["type"], "string")
            self.assertEqual(item["minLength"], 1)
            self.assertEqual(item["pattern"], r"\S")
            self.assertNotIn("maxLength", item)

        long_identity = {
            "opener": "أ" * 900,
            "closer": "ب" * 900,
            "transitions": ["ج" * 300, "د" * 300, "هـ" * 300],
        }
        normalized = validate_narrative_identity(long_identity)
        self.assertEqual(len(normalized["opener"]), 600)
        self.assertEqual(len(normalized["closer"]), 600)
        self.assertTrue(all(len(item) == 200 for item in normalized["transitions"]))

    def test_prompt_and_validator_agree_on_exactly_three_transitions(self) -> None:
        prompt = _narrative_identity_prompt(
            brief=_brief(),
            plan=_plan(),
            canonical_opener="افتتاحية ثابتة للاختبار",
            canonical_closer="خاتمة ثابتة للاختبار",
        )
        self.assertIn("exactly 3 short natural Arabic transition phrases", prompt)
        with self.assertRaisesRegex(
            ContractError,
            "requires exactly 3 transitions",
        ):
            validate_narrative_identity(
                {
                    "opener": "افتتاحية",
                    "closer": "خاتمة",
                    "transitions": ["واحدة", "اثنتان"],
                }
            )

    def test_mistral_narrative_identity_call_passes_strict_schema(self) -> None:
        prompt = _narrative_identity_prompt(
            brief=_brief(),
            plan=_plan(),
            canonical_opener="افتتاحية ثابتة للاختبار",
            canonical_closer="خاتمة ثابتة للاختبار",
        )
        expected = {
            "opener": "افتتاحية جديدة",
            "closer": "خاتمة جديدة",
            "transitions": ["الأولى", "الثانية", "الثالثة"],
        }

        with mock.patch.object(
            providers_module.mistral_executor,
            "mistral_executor_json",
            return_value=expected,
        ) as called:
            result = providers_module._mistral_call(
                prompt,
                900,
                "narrative_identity",
            )

        self.assertEqual(result, expected)
        kwargs = called.call_args.kwargs
        self.assertEqual(kwargs["task_kind"], "narrative_identity")
        self.assertEqual(kwargs["max_tokens"], 900)
        self.assertEqual(
            kwargs["response_schema"],
            (
                "narrative_identity",
                providers_module.MISTRAL_NARRATIVE_IDENTITY_SCHEMA,
            ),
        )
        self.assertEqual(validate_narrative_identity(result)["transitions"], expected["transitions"])


class MistralScriptSchemaTests(unittest.TestCase):
    def test_script_schema_is_derived_from_locked_plan_with_exact_order(self) -> None:
        prompt = _script_prompt(_brief(), _plan())
        schema = providers_module._mistral_script_response_schema(prompt)

        self.assertEqual(schema["required"], ["title", "sections"])
        self.assertFalse(schema["additionalProperties"])
        sections = schema["properties"]["sections"]
        self.assertEqual(sections["minItems"], 5)
        self.assertEqual(sections["maxItems"], 5)
        self.assertEqual(
            [item["properties"]["id"]["const"] for item in sections["prefixItems"]],
            ["s1", "s2", "s3", "s4", "s5"],
        )
        for item in sections["prefixItems"]:
            self.assertEqual(item["required"], ["id", "narration"])
            self.assertFalse(item["additionalProperties"])
            self.assertEqual(item["properties"]["narration"]["minLength"], 20)

    def test_mistral_script_call_passes_strict_schema_to_executor(self) -> None:
        prompt = _script_prompt(_brief(), _plan())
        expected = {"title": "خطوة واحدة", "sections": _script()["sections"]}

        with mock.patch.object(
            providers_module.mistral_executor,
            "mistral_executor_json",
            return_value=expected,
        ) as called:
            result = providers_module._mistral_call(prompt, 7500, "script")

        self.assertEqual(result, expected)
        kwargs = called.call_args.kwargs
        self.assertEqual(kwargs["task_kind"], "script")
        self.assertEqual(kwargs["max_tokens"], 7500)
        name, schema = kwargs["response_schema"]
        self.assertEqual(name, "script")
        self.assertEqual(
            [item["properties"]["id"]["const"] for item in schema["properties"]["sections"]["prefixItems"]],
            ["s1", "s2", "s3", "s4", "s5"],
        )
        self.assertEqual(schema["properties"]["sections"]["minItems"], 5)
        self.assertEqual(schema["properties"]["sections"]["maxItems"], 5)

    def test_script_schema_context_failure_is_no_wire(self) -> None:
        with mock.patch.object(
            providers_module.mistral_executor,
            "mistral_executor_json",
        ) as called:
            with self.assertRaisesRegex(
                NoWireFailure,
                "mistral_script_locked_plan_missing",
            ):
                providers_module._mistral_call(
                    "script prompt without locked plan",
                    7500,
                    "script",
                )
        called.assert_not_called()


class MistralScriptDiagnosticsTests(unittest.TestCase):
    def test_script_validator_logs_safe_raw_shape_without_narration_text(self) -> None:
        plan = _plan()
        private_marker = "PRIVATE_NARRATION_MUST_NOT_BE_LOGGED"
        reversed_ids = [item["id"] for item in reversed(plan["sections"])]
        raw_value = {
            "title": "private title",
            "sections": [
                {
                    "id": section_id,
                    "narration": private_marker + (" x" * 20),
                }
                for section_id in reversed_ids
            ],
        }
        raw_content = json.dumps(raw_value, ensure_ascii=False)

        def mistral_call(_prompt, _tokens, stage):
            self.assertEqual(stage, "script")
            return raw_value

        router = ProviderRouter(
            (
                ProviderAdapter(
                    "mistral",
                    mistral_call,
                    stages=frozenset({"script"}),
                    accepts_stage=True,
                ),
            )
        )
        with mock.patch.object(
            providers_module.mistral_executor,
            "get_last_mistral_executor_raw_content",
            return_value=raw_content,
        ), mock.patch("builtins.print") as logged:
            with self.assertRaisesRegex(
                RuntimeError,
                "mistral:invalid_output_contracterror",
            ):
                router.route(
                    stage="script",
                    prompt="safe script prompt",
                    max_tokens=7500,
                    validator=lambda value: validate_script(value, plan),
                )

        log_text = "\n".join(str(call.args[0]) for call in logged.call_args_list)
        self.assertIn("Mistral script validator rejected raw content", log_text)
        self.assertIn("script section ids/order must match plan exactly", log_text)
        self.assertIn('"narration_chars"', log_text)
        self.assertIn('"sha256"', log_text)
        self.assertIn(reversed_ids[0], log_text)
        self.assertNotIn(private_marker, log_text)
        self.assertNotIn("private title", log_text)


class MistralScriptPatchDiagnosticsTests(unittest.TestCase):
    """Run #315: OpenRouter's tone/naturalness audit cited quoted example
    fragments (garbled mixed-script text) that never appeared anywhere in the
    actual script. When those flags were flattened verbatim into the tone
    repair prompt, the repair provider had no way to know the quotes were
    unreliable, and any patch built from one fails the local find/replace
    validator's exact-match check regardless of which provider produced it.
    """

    def test_run315_hallucinated_quote_find_rejected_by_real_validator(self) -> None:
        plan = _plan()
        script = _script()
        identity = {"opener": "", "closer": "", "transitions": []}
        cta_plan = {"anchor_section_id": "", "spoken_text": ""}
        hallucinated_find = "نص-غير-موجود-إطلاقا"
        self.assertNotIn(hallucinated_find, script["sections"][1]["narration"])

        value = {
            "patches": [
                {
                    "section_id": "s2",
                    "find": hallucinated_find,
                    "replace": "نص بديل",
                }
            ]
        }
        with self.assertRaisesRegex(
            ValueError,
            "script patch find text must match exactly once",
        ):
            _validate_and_apply_script_patches(
                value,
                plan=plan,
                original_script=script,
                identity=identity,
                cta_plan=cta_plan,
                revision_note="- [tone] s2: garbled fragment noted",
            )

    def test_script_patch_validator_logs_safe_raw_shape_without_find_replace_text(
        self,
    ) -> None:
        plan = _plan()
        script = _script()
        identity = {"opener": "", "closer": "", "transitions": []}
        cta_plan = {"anchor_section_id": "", "spoken_text": ""}
        private_find = "PRIVATE_FIND_TEXT_MUST_NOT_BE_LOGGED"
        raw_value = {
            "patches": [
                {
                    "section_id": "s2",
                    "find": private_find,
                    "replace": "بديل غير حساس",
                }
            ]
        }
        raw_content = json.dumps(raw_value, ensure_ascii=False)

        def mistral_call(_prompt, _tokens, stage):
            self.assertEqual(stage, "script_patch")
            return raw_value

        router = ProviderRouter(
            (
                ProviderAdapter(
                    "mistral",
                    mistral_call,
                    stages=frozenset({"script_patch"}),
                    accepts_stage=True,
                ),
            )
        )
        with mock.patch.object(
            providers_module.mistral_executor,
            "get_last_mistral_executor_raw_content",
            return_value=raw_content,
        ), mock.patch("builtins.print") as logged:
            with self.assertRaisesRegex(
                RuntimeError,
                "mistral:invalid_output_valueerror",
            ):
                router.route(
                    stage="script_patch",
                    prompt="safe script_patch prompt",
                    max_tokens=2200,
                    validator=lambda value: _validate_and_apply_script_patches(
                        value,
                        plan=plan,
                        original_script=script,
                        identity=identity,
                        cta_plan=cta_plan,
                        revision_note="- [tone] s2: garbled fragment noted",
                    ),
                )

        log_text = "\n".join(str(call.args[0]) for call in logged.call_args_list)
        self.assertIn("Mistral script_patch validator rejected raw content", log_text)
        self.assertIn("script patch find text must match exactly once", log_text)
        self.assertIn('"find_chars"', log_text)
        self.assertIn('"sha256"', log_text)
        self.assertIn("s2", log_text)
        self.assertNotIn(private_find, log_text)


class ToneRepairFlagQuoteVerificationTests(unittest.TestCase):
    """Run #315 root cause: `_tone_repair_issue_notes` must not pass a
    quoted example an auditor cites straight into the repair prompt unless
    that exact text actually exists in the script being repaired.
    """

    def test_run315_fabricated_quote_dropped_while_real_quote_and_plain_flag_survive(
        self,
    ) -> None:
        script = _script()
        real_excerpt = "حين نصغر الفعل الأول"
        self.assertIn(real_excerpt, script["sections"][1]["narration"])
        fabricated_excerpt = "الخططatego执行ية"
        self.assertNotIn(fabricated_excerpt, _script_text_haystack(script))

        report = {
            "naturalness_flags": [
                f"Script contains garbled fragments, e.g., '{fabricated_excerpt}', "
                "breaking Modern Standard Arabic naturalness.",
                f"Repeats the phrase '{real_excerpt}' awkwardly in s2.",
                "Missing punctuation and run-on sentences reduce readability.",
            ],
        }

        notes = _tone_repair_issue_notes(report, script)

        self.assertNotIn(fabricated_excerpt, notes)
        self.assertIn(real_excerpt, notes)
        self.assertIn("Missing punctuation and run-on sentences", notes)

    def test_no_script_supplied_keeps_legacy_unfiltered_behavior(self) -> None:
        fabricated_excerpt = "نص لن يوجد أبدًا في أي سكربت"
        report = {"naturalness_flags": [f"garbled example '{fabricated_excerpt}'"]}
        notes = _tone_repair_issue_notes(report)
        self.assertIn(fabricated_excerpt, notes)


class RepairPatchSpanBrevityGuidanceTests(unittest.TestCase):
    """Run #342: a schema-valid Mistral script_patch response still failed the
    exact-match validator because at least one `find` span sat at/near the
    400-char schema ceiling - long verbatim copies are far more likely to
    contain a transcription slip. Both repair prompts must steer the provider
    toward the shortest sufficient span instead of whole-sentence copies.
    """

    BREVITY_GUIDANCE = "Keep patch.find as SHORT as possible"

    def test_tone_repair_prompt_asks_for_short_find_spans(self) -> None:
        identity = {"opener": "", "closer": "", "transitions": ["أولاً", "ثم", "أخيرًا"]}
        cta_plan = {"anchor_section_id": "", "spoken_text": ""}
        prompt = _tone_repair_prompt(
            brief=_brief(),
            plan=_plan(),
            script=_script(),
            identity=identity,
            cta_plan=cta_plan,
            revision_note="- [tone] s2: garbled fragment noted",
        )
        self.assertIn(self.BREVITY_GUIDANCE, prompt)

    def test_factuality_repair_prompt_asks_for_short_find_spans(self) -> None:
        identity = {"opener": "", "closer": "", "transitions": ["أولاً", "ثم", "أخيرًا"]}
        cta_plan = {"anchor_section_id": "", "spoken_text": ""}
        prompt = _factuality_repair_prompt(
            brief=_brief(),
            plan=_plan(),
            script=_script(),
            identity=identity,
            cta_plan=cta_plan,
            revision_note="- [factuality] s2: guarantee exceeds evidence",
        )
        self.assertIn(self.BREVITY_GUIDANCE, prompt)


class RepairAddressEveryFlagGuidanceTests(unittest.TestCase):
    """Run #20: the tone audit raised three independent flags (a preachy,
    unprepared closing; an informal word 'أستنى'; and an awkward phrase
    'أظل أُقف', both inside the hook). The accepted repair (Groq) fixed only
    the closing and left the two hook-area naturalness flags untouched. The
    mandatory full re-audit that follows still blocked on those two, and the
    strict one-shot policy then failed the run - even though the model had
    every permission it needed (s1 was an allowed patch target and the
    schema allows up to 6 patches) to fix all three in the same response.
    Both repair prompts must say explicitly that every listed flag - not
    just one - must be addressed in the single attempt.
    """

    ADDRESS_EVERY_FLAG_GUIDANCE = "Fix EVERY concrete"

    def test_tone_repair_prompt_asks_to_address_every_flag(self) -> None:
        identity = {"opener": "", "closer": "", "transitions": ["أولاً", "ثم", "أخيرًا"]}
        cta_plan = {"anchor_section_id": "", "spoken_text": ""}
        prompt = _tone_repair_prompt(
            brief=_brief(),
            plan=_plan(),
            script=_script(),
            identity=identity,
            cta_plan=cta_plan,
            revision_note="- [tone] s2: garbled fragment noted",
        )
        self.assertIn(self.ADDRESS_EVERY_FLAG_GUIDANCE, prompt)
        self.assertIn("not just one", prompt)

    def test_factuality_repair_prompt_asks_to_address_every_flag(self) -> None:
        identity = {"opener": "", "closer": "", "transitions": ["أولاً", "ثم", "أخيرًا"]}
        cta_plan = {"anchor_section_id": "", "spoken_text": ""}
        prompt = _factuality_repair_prompt(
            brief=_brief(),
            plan=_plan(),
            script=_script(),
            identity=identity,
            cta_plan=cta_plan,
            revision_note="- [factuality] s2: guarantee exceeds evidence",
        )
        self.assertIn(self.ADDRESS_EVERY_FLAG_GUIDANCE, prompt)
        self.assertIn("not just one", prompt)


class AuditNarrativeFormatAvoidsDialogueConfusionTests(unittest.TestCase):
    """Runs #17 and #22: the frozen Engine's reused tone audit was sent
    narrative_format="inner_dialogue" verbatim - a Clean V2-only Short
    template label the Engine's own audit prompt was never taught, whose
    name literally contains the word "dialogue". Both runs blocked wholly
    correct single-voice inner narration with "content is a monologue, not
    dialogue". The legacy/Long path never hits this because it always sends
    the Engine's own known "direct_cinematic" value. Send the audit an
    unambiguous equivalent label for inner_dialogue specifically; every
    other Clean V2 template name (used by the Engine's audit call) and the
    Clean V2-internal template name (used everywhere else: prompts,
    contracts, manifests) are unaffected.
    """

    def test_inner_dialogue_short_is_relabeled_for_the_audit_only(self) -> None:
        brief = {
            "approved_by_user": True,
            "approved_topic": "كيف تنهض عندما تفقد الدافع تمامًا؟",
            "format": "short",
            "language": "ar",
            "audience": "Arabic-speaking adults",
            "editorial_intent": "نبرة هادئة وطبيعية.",
            "research_pack": [],
            "hard_constraints": ["No fabricated facts."],
        }
        self.assertEqual(select_short_template(brief)["template"], "inner_dialogue")
        audit_format = _audit_narrative_format_for_brief(brief)
        self.assertEqual(audit_format, "inner_monologue")
        self.assertNotIn("dialogue", audit_format)

    def test_other_short_templates_are_unaffected(self) -> None:
        brief = {
            "approved_by_user": True,
            "approved_topic": "لماذا نؤجل الأشياء المهمة؟",
            "format": "short",
            "language": "ar",
            "audience": "Arabic-speaking adults",
            "editorial_intent": "نبرة هادئة وطبيعية.",
            "research_pack": [],
            "hard_constraints": ["No fabricated facts."],
        }
        template = select_short_template(brief)["template"]
        self.assertNotEqual(template, "inner_dialogue")
        self.assertEqual(_audit_narrative_format_for_brief(brief), template)

    def test_long_format_keeps_legacy_direct_cinematic(self) -> None:
        brief = {
            "approved_by_user": True,
            "approved_topic": "كيف تبدأ بخطوة صغيرة",
            "format": "film",
            "language": "ar",
            "audience": "Arabic-speaking adults",
            "editorial_intent": "شرح عملي هادئ.",
            "research_pack": [],
            "hard_constraints": ["No fabricated facts."],
        }
        self.assertEqual(_audit_narrative_format_for_brief(brief), "direct_cinematic")

    def test_internal_template_name_used_elsewhere_stays_inner_dialogue(self) -> None:
        brief = {
            "approved_by_user": True,
            "approved_topic": "كيف تنهض عندما تفقد الدافع تمامًا؟",
            "format": "short",
            "language": "ar",
            "audience": "Arabic-speaking adults",
            "editorial_intent": "نبرة هادئة وطبيعية.",
            "research_pack": [],
            "hard_constraints": ["No fabricated facts."],
        }
        # The relabel is audit-facing only; the template used for prompts,
        # contracts, and manifests must still resolve to inner_dialogue.
        self.assertEqual(select_short_template(brief)["template"], "inner_dialogue")


class ProviderAccountingTests(unittest.TestCase):
    def test_local_unavailable_route_is_no_wire_and_does_not_take_attempt_number(self) -> None:
        def missing(_prompt: str, _tokens: int) -> dict:
            raise NoWireFailure("missing_api_key")

        router = ProviderRouter(
            (
                ProviderAdapter("missing", missing),
                ProviderAdapter("working", lambda _prompt, _tokens: {"ok": True}),
            )
        )
        result = router.route(
            stage="planning",
            prompt="small",
            max_tokens=100,
            validator=lambda value: value,
        )
        self.assertEqual(result, {"ok": True})
        self.assertFalse(router.events[0]["wire_attempted"])
        self.assertIsNone(router.events[0]["provider_attempt"])
        self.assertIsNone(router.events[0]["stage_wire_attempt"])
        self.assertTrue(router.events[1]["wire_attempted"])
        self.assertEqual(router.events[1]["stage_wire_attempt"], 1)

    def test_invalid_post_wire_output_counts_then_falls_forward_once(self) -> None:
        router = ProviderRouter(
            (
                ProviderAdapter("bad", lambda _prompt, _tokens: {"wrong": True}),
                ProviderAdapter("good", lambda _prompt, _tokens: {"ok": True}),
            )
        )

        def validate(value: dict) -> dict:
            if value.get("ok") is not True:
                raise ContractError("bad shape")
            return value

        self.assertEqual(
            router.route(
                stage="script",
                prompt="small",
                max_tokens=100,
                validator=validate,
            ),
            {"ok": True},
        )
        self.assertEqual(
            [event["stage_wire_attempt"] for event in router.events], [1, 2]
        )
        self.assertEqual(router.events[0]["result"], "invalid_output")

    def test_oversized_prompt_is_a_single_local_no_wire_block(self) -> None:
        router = ProviderRouter(
            (ProviderAdapter("unused", lambda _prompt, _tokens: {"ok": True}),)
        )
        with self.assertRaisesRegex(NoWireFailure, "prompt_too_large"):
            router.route(
                stage="planning",
                prompt="x" * (65 * 1024),
                max_tokens=100,
                validator=lambda value: value,
            )
        self.assertEqual(len(router.events), 1)
        self.assertFalse(router.events[0]["wire_attempted"])
        self.assertIsNone(router.events[0]["provider_attempt"])


class WorkflowContractTests(unittest.TestCase):
    WORKFLOW = Path(".github/workflows/clean-v2-minimal-e2e.yml")

    def test_workflow_uses_frozen_engine_and_approved_brief(self) -> None:
        text = self.WORKFLOW.read_text(encoding="utf-8")
        engine_sha = "3cbd689819e6b0e0b2ea9904d1998e24a5e2a293"
        brief_sha = "bcf8d3017ee8e18ee4808614c7c07731b0452a5ba6182e1183404f663078e129"
        self.assertGreaterEqual(text.count(engine_sha), 2)
        self.assertEqual(text.count(brief_sha), 1)
        self.assertIn("engine/production/approved_brief.json", text)
        self.assertIn("python -m clean_v2", text)
        self.assertIn("لماذا تفشل خطط إدارة الوقت في الحياة اليومية", text)

    def test_content_fallback_pins_specific_free_openrouter_model(self) -> None:
        workflow = self.WORKFLOW.read_text(encoding="utf-8")
        providers = Path("clean_v2/providers.py").read_text(encoding="utf-8")
        expected = "google/gemma-4-26b-a4b-it:free"
        self.assertIn(f"OPENROUTER_CONTENT_MODEL: {expected}", workflow)
        self.assertNotIn("OPENROUTER_CONTENT_MODEL: openrouter/free", workflow)
        self.assertIn(f'or "{expected}"', providers)
        self.assertIn(f'if model != "{expected}":', providers)
        self.assertNotIn('{"openrouter/free", "google/gemma-4-26b-a4b-it:free"}', providers)

    def test_workflow_persists_exact_sha_pre_qc_resume_checkpoint(self) -> None:
        text = self.WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("Restore Clean V2 pre-QC checkpoint", text)
        self.assertIn("Save Clean V2 pre-QC checkpoint", text)
        self.assertIn('CLEAN_V2_RESUME:', text)
        self.assertIn('--resume-from "$CLEAN_V2_RESUME"', text)
        self.assertIn("clean-v2-pre-qc-${{ runner.os }}-${{ github.sha }}", text)
        self.assertIn("${{ env.ISCO_ENGINE_SHA }}", text)
        self.assertIn("${{ env.ISCO_APPROVED_BRIEF_SHA256 }}", text)

    def test_workflow_invokes_security_cinematic_then_final_master_without_legacy_orchestrator(self) -> None:
        text = self.WORKFLOW.read_text(encoding="utf-8").casefold()
        self.assertIn("final-master-qc.json", text)
        self.assertIn("security-cinematic-v2.json", text)
        self.assertIn("tesseract-ocr", text)
        self.assertIn("fonts-noto-core", text)
        self.assertIn("pythonpath", text)
        for forbidden in (
            "produce-resilient-v4",
            "run_v3_voice",
            "gold_enforce",
            "viewer_quality",
            "create release",
        ):
            self.assertNotIn(forbidden, text)


def _passing_visual_qa(**kwargs) -> dict:
    output_dir = Path(kwargs["output_dir"])
    report = {
        "schema_version": 1,
        "layer": VISUAL_QA_STAGE,
        "status": "pass",
        "repair_or_replacement_enabled": False,
    }
    (output_dir / "visual-audit.json").write_text(
        json.dumps(
            [
                {
                    "section": "s1",
                    "status": "pass",
                    "is_selected": True,
                    "relevance": 0.9,
                    "visual_quality": 0.9,
                }
            ]
        ),
        encoding="utf-8",
    )
    (output_dir / "final-cut-visual-qa.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    return report


def _mutating_visual_qa(**kwargs) -> dict:
    output_dir = Path(kwargs["output_dir"])
    rights = kwargs["rights"]
    first = rights[0]
    visual = output_dir / "visuals" / str(first["local_file"])
    visual.write_bytes(visual.read_bytes() + b"semantic-recovery")
    first["asset_id"] = "recovered-asset"
    first["query"] = "narration-bound alternate query"
    (output_dir / "rights-manifest.json").write_text(
        json.dumps({"schema_version": 1, "assets": rights}),
        encoding="utf-8",
    )
    report = {
        "schema_version": 1,
        "layer": VISUAL_QA_STAGE,
        "status": "pass",
        "repair_or_replacement_enabled": True,
        "semantic_recovery_count": 1,
        "final_media_mutated": True,
    }
    (output_dir / "visual-audit.json").write_text("[]", encoding="utf-8")
    (output_dir / "final-cut-visual-qa.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    return report


def _blocking_visual_qa(**kwargs) -> dict:
    output_dir = Path(kwargs["output_dir"])
    (output_dir / "visual-audit.json").write_text("[]", encoding="utf-8")
    raise RuntimeError(
        "CLEAN_V2_VISUAL_QA_BLOCK section=s1 reason=selected_visual_not_final_cut_ready"
    )


def _infrastructure_visual_qa(**kwargs) -> dict:
    raise RuntimeError(
        "CLEAN_V2_VISUAL_QA_INFRASTRUCTURE section=s1 error_type=VisionProviderMeshUnavailableError"
    )


def _blocking_opening_director(**kwargs) -> dict:
    raise RuntimeError(
        "CLEAN_V2_OPENING_BLOCK reason=two_opening_auxiliaries_not_final_cut_ready"
    )


def _passing_text_audit(**kwargs) -> dict:
    output_dir = Path(kwargs["output_dir"])
    report = {
        "schema_version": 1,
        "source": "clean-v2-legacy-factuality-audit",
        "status": "pass",
        "unsupported_claims": [],
        "professional_advice_flags": [],
        "expert_persona_flags": [],
        "notes": [],
    }
    (output_dir / "factuality-audit.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    return report


def _blocking_text_audit(**kwargs) -> dict:
    output_dir = Path(kwargs["output_dir"])
    report = {
        "schema_version": 1,
        "source": "clean-v2-legacy-factuality-audit",
        "status": "block",
        "unsupported_claims": ["fixture_unsupported_claim"],
        "professional_advice_flags": [],
        "expert_persona_flags": [],
        "notes": [],
    }
    (output_dir / "factuality-audit.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    raise RuntimeError("Independent factuality/AI-expert gate blocked real production")


def _infrastructure_text_audit(**kwargs) -> dict:
    raise RuntimeError(
        "text_audit exhausted bounded provider route: "
        "gemini:http_429, groq:http_429, openrouter:http_429"
    )


def _passing_audio_mastering(**kwargs) -> dict:
    output_dir = Path(kwargs["output_dir"])
    narration_path = Path(kwargs["narration_path"])
    mastered_path = output_dir / "narration-mastered.wav"
    shutil.copyfile(narration_path, mastered_path)
    report = {
        "schema_version": 1,
        "source": "clean-v2-audio-loudness-mastering",
        "narration_file": mastered_path.name,
        "status": "pass",
        "target_integrated_lufs": -16.0,
        "target_true_peak_dbtp": -1.5,
        "target_loudness_range": 11.0,
        "alimiter_ceiling_linear": 0.84,
        "measured_input_integrated_lufs": -20.0,
        "measured_input_true_peak_dbtp": -6.0,
    }
    (output_dir / "audio-mastering.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    return report


def _failing_audio_mastering(**kwargs) -> dict:
    raise RuntimeError("audio_loudness_measurement_unparseable")


def _passing_narrative_identity(**kwargs) -> dict:
    output_dir = Path(kwargs["output_dir"])
    report = {
        "schema_version": 1,
        "source": "clean-v2-narrative-identity",
        "canonical_opener": "أهلاً بكم من جديد في نداء اليقظة",
        "canonical_closer": "إلى لقاء قادم في نداء اليقظة",
        "opener": "أهلاً بكم من جديد في هذه الحلقة من نداء اليقظة",
        "closer": "نلقاكم في حلقة قادمة من نداء اليقظة",
        "transitions": ["بعد هذه الفكرة", "ولننتقل الآن", "وهنا يأتي السؤال"],
    }
    (output_dir / "narrative-identity.json").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8"
    )
    return report


def _infrastructure_narrative_identity(**kwargs) -> dict:
    raise RuntimeError(
        f"{IDENTITY_STAGE} exhausted bounded provider route: "
        "gemini:http_429, groq:http_429, openrouter:http_429"
    )


def _passing_cinematic_layer(**kwargs) -> dict:
    output_dir = Path(kwargs["output_dir"])
    report = {
        "schema_version": 1,
        "layer": CINEMATIC_STAGE,
        "status": "pass",
        "reuse_not_rewrite": True,
        "ai_calls_added": 0,
    }
    (output_dir / "security-cinematic-v2.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    return report


def _blocking_cinematic_layer(**kwargs) -> dict:
    output_dir = Path(kwargs["output_dir"])
    report = {
        "schema_version": 1,
        "layer": CINEMATIC_STAGE,
        "status": "block",
    }
    (output_dir / "security-cinematic-v2.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    raise RuntimeError("synthetic new layer block")


def _passing_final_master_qc(output_dir: Path) -> dict:
    report = {
        "schema_version": 1,
        "status": "pass",
        "production_stage": "post_render_pre_gold_acceptance",
        "ai_calls_added": 0,
        "final_media_mutated": False,
        "blocking_findings": [],
        "warnings": [],
    }
    (output_dir / "final-master-qc.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    return report


def _blocking_final_master_qc(output_dir: Path) -> dict:
    report = {
        "schema_version": 1,
        "status": "block",
        "production_stage": "post_render_pre_gold_acceptance",
        "ai_calls_added": 0,
        "final_media_mutated": False,
        "blocking_findings": ["fixture_block"],
        "warnings": [],
    }
    (output_dir / "final-master-qc.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    raise RuntimeError("Final Master QC blocked release")


class _FakeRouter:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def route(self, *, stage, prompt, max_tokens, validator):
        del prompt, max_tokens
        value = _plan() if stage == "planning" else _script()
        self.events.append(
            {
                "stage": stage,
                "provider": "fixture",
                "result": "success",
                "wire_attempted": True,
                "provider_attempt": 1,
                "stage_wire_attempt": 1,
            }
        )
        return validator(value)


class _InfrastructureRouter:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def route(self, *, stage, prompt, max_tokens, validator):
        del prompt, max_tokens, validator
        raise RuntimeError(
            f"{stage} exhausted bounded provider route: "
            "gemini:http_429, groq:http_429, openrouter:http_429"
        )


class _FakeVoice:
    def __init__(self) -> None:
        self.calls = 0
        self.last_provider = "nabra:af_msa"
        self.fallback_used = True

    def synthesize(self, transcript: str, output_path: Path) -> Path:
        self.calls += 1
        if not transcript.strip():
            raise RuntimeError("empty fixture transcript")
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                # Five section-scoped calls must preserve the fixture's historical
                # ~3 second total narration duration (5 * 0.6s).
                "sine=frequency=220:sample_rate=24000:duration=0.6",
                "-c:a",
                "pcm_s16le",
                "-y",
                str(output_path),
            ],
            check=True,
        )
        return output_path


class _FakeVisuals:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.calls = 0

    def acquire(self, plan, output_dir, fmt, max_visuals, section_estimated_seconds=None):
        self.calls += 1
        del plan, fmt, max_visuals, section_estimated_seconds
        output_dir.mkdir(parents=True, exist_ok=True)
        clips = []
        for index, color in enumerate(("#172033", "#6d4c41"), start=1):
            path = output_dir / f"fixture-{index}.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    f"color=c={color}:s=640x360:r=30:d=2",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-an",
                    "-y",
                    str(path),
                ],
                check=True,
            )
            clips.append(path)
        self.events.append(
            {
                "provider": "fixture",
                "result": "selected",
                "wire_attempted": False,
            }
        )
        rights = [
            {
                "provider": "fixture",
                "asset_id": str(index),
                "local_file": path.name,
            }
            for index, path in enumerate(clips, start=1)
        ]
        return clips, rights


class _StuckQueryVisuals:
    """Mirrors the real Security V1 query normalizer's failure signature for a
    visual_query_en baked into plan.json that never validates - task #21."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.calls = 0

    def acquire(self, plan, output_dir, fmt, max_visuals, section_estimated_seconds=None):
        self.calls += 1
        del plan, output_dir, fmt, max_visuals, section_estimated_seconds
        raise RuntimeError(
            "CLEAN_V2_NEW_LAYER_BLOCK stage=security_v1.query "
            "error=ModelOutputSchemaError:visual_query_not_plain_english_search_terms"
        )


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class CleanV2EndToEndTests(unittest.TestCase):
    def test_minimal_path_produces_structurally_complete_final_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            result = pipeline.run(
                brief_path=brief_path,
                approved_sha256=compute_brief_sha256(brief),
                output_dir=output,
                engine_sha="a" * 40,
                runner_sha="b" * 40,
                max_visuals=2,
            )

            self.assertEqual(result["status"], "pass")
            self.assertTrue((output / "final.mp4").is_file())
            manifest = json.loads(
                (output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "pass")
            self.assertEqual([item["name"] for item in manifest["stages"]], list(STAGES))
            self.assertEqual(
                manifest["quality_layers_executed"],
                [TEXT_AUDIT_STAGE, CINEMATIC_STAGE, VISUAL_QA_STAGE, OPENING_STAGE, "final_master_qc"],
            )
            self.assertEqual(manifest["text_audit_status"], "pass")
            cta_report = json.loads((output / "cta-plan.json").read_text(encoding="utf-8"))
            self.assertEqual(cta_report["rules"]["provider_calls"], 0)
            self.assertEqual(cta_report["provider_calls_added"], 0)
            self.assertEqual(cta_report["mode"], "comment")
            self.assertEqual(manifest["opening_director_status"], "not_applicable")
            self.assertEqual(manifest["cinematic_v2_status"], "pass")
            self.assertEqual(manifest["final_master_qc_status"], "pass")
            final = json.loads((output / "final.json").read_text(encoding="utf-8"))
            self.assertEqual(final["status"], "pass")
            self.assertGreaterEqual(final["video_streams"], 1)
            self.assertGreaterEqual(final["audio_streams"], 1)
            qc_report = json.loads(
                (output / "final-master-qc.json").read_text(encoding="utf-8")
            )
            self.assertEqual(qc_report["status"], "pass")
            forbidden = {"gold", "text-audit", "viewer-quality"}
            names = {path.name.lower() for path in output.rglob("*")}
            self.assertFalse(any(any(token in name for token in forbidden) for name in names))

    def test_pre_qc_resume_skips_provider_voice_and_visual_rework(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(
                json.dumps(brief, ensure_ascii=False), encoding="utf-8"
            )
            approved = compute_brief_sha256(brief)
            first_output = root / "first"
            first_router = _FakeRouter()
            first_voice = _FakeVoice()
            first_visuals = _FakeVisuals()
            first = CleanV2Pipeline(
                router=first_router,
                voice_synthesizer=first_voice,
                visual_source=first_visuals,
                visual_qa=_infrastructure_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(
                RuntimeError, "CLEAN_V2_VISUAL_QA_INFRASTRUCTURE"
            ):
                first.run(
                    brief_path=brief_path,
                    approved_sha256=approved,
                    output_dir=first_output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )

            checkpoint = json.loads(
                (first_output / "resume-checkpoint.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(checkpoint["completed_stage"], "visuals")
            self.assertEqual(
                [event["stage"] for event in first_router.events],
                ["planning", "script"],
            )
            # Timeline First intentionally synthesizes measured semantic voice
            # units (hook/prayer/channel identity/topic/outro). Lock the test to the
            # persisted unit manifest instead of the old one-extra-hook-chunk count.
            voice_manifest = json.loads(
                (first_output / "voice-sections.json").read_text(encoding="utf-8")
            )
            expected_voice_calls = sum(
                int(section.get("chunk_count") or 0)
                for section in voice_manifest.get("sections", [])
            )
            self.assertGreater(expected_voice_calls, len(_script()["sections"]))
            self.assertEqual(first_voice.calls, expected_voice_calls)
            self.assertEqual(first_visuals.calls, 1)

            class _ForbiddenRouter:
                events: list[dict] = []

                def route(self, **_kwargs):
                    raise AssertionError("provider route must be resumed")

            class _ForbiddenVoice:
                def synthesize(self, *_args, **_kwargs):
                    raise AssertionError("voice must be resumed")

            class _ForbiddenVisuals:
                events: list[dict] = []

                def acquire(self, *_args, **_kwargs):
                    raise AssertionError("visual acquisition must be resumed")

            second_output = root / "second"
            second = CleanV2Pipeline(
                router=_ForbiddenRouter(),
                voice_synthesizer=_ForbiddenVoice(),
                visual_source=_ForbiddenVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            result = second.run(
                brief_path=brief_path,
                approved_sha256=approved,
                output_dir=second_output,
                engine_sha="a" * 40,
                runner_sha="b" * 40,
                max_visuals=2,
                resume_from=first_output,
            )
            self.assertEqual(result["status"], "pass")
            manifest = json.loads(
                (second_output / "run-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                manifest["resumed_stages"],
                ["planning", IDENTITY_STAGE, "script", "voice", "visuals"],
            )
            self.assertTrue(manifest["resume_checkpoint_accepted"])
            self.assertEqual(manifest["resume_completed_stage"], "visuals")
            resumed_by_name = {
                stage["name"]: stage.get("resumed") for stage in manifest["stages"]
            }
            for name in ("planning", IDENTITY_STAGE, "script", "voice", "visuals"):
                self.assertTrue(resumed_by_name[name])
            self.assertFalse(resumed_by_name[TEXT_AUDIT_STAGE])

    def test_tampered_resume_checkpoint_fails_closed_to_normal_routing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(
                json.dumps(brief, ensure_ascii=False), encoding="utf-8"
            )
            approved = compute_brief_sha256(brief)
            first_output = root / "first"
            first = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_infrastructure_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(
                RuntimeError, "CLEAN_V2_VISUAL_QA_INFRASTRUCTURE"
            ):
                first.run(
                    brief_path=brief_path,
                    approved_sha256=approved,
                    output_dir=first_output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )
            (first_output / "plan.json").write_text(
                '{"tampered":true}\n', encoding="utf-8"
            )

            second_output = root / "second"
            second = CleanV2Pipeline(
                router=_InfrastructureRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(
                RuntimeError, "planning exhausted bounded provider route"
            ):
                second.run(
                    brief_path=brief_path,
                    approved_sha256=approved,
                    output_dir=second_output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                    resume_from=first_output,
                )
            manifest = json.loads(
                (second_output / "run-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["resumed_stages"], [])
            self.assertNotIn("resume_checkpoint_accepted", manifest)

    def test_visuals_content_block_invalidates_resume_checkpoint(self) -> None:
        # Task #21: a plan/script whose visual_query_en fails Security V1's
        # content check at the "visuals" stage must not be handed to future
        # attempts via the resume checkpoint - otherwise every resumed retry
        # keeps re-inheriting, and re-saving, the exact same unusable output
        # forever, reproducing the stuck-query pattern observed across three
        # separate cohort attempts (#93, #94, #98).
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(
                json.dumps(brief, ensure_ascii=False), encoding="utf-8"
            )
            approved = compute_brief_sha256(brief)

            first_output = root / "first"
            first = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_StuckQueryVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(RuntimeError, "CLEAN_V2_NEW_LAYER_BLOCK"):
                first.run(
                    brief_path=brief_path,
                    approved_sha256=approved,
                    output_dir=first_output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )

            # The "voice" checkpoint (the last stage that did succeed) must not
            # survive a genuine content block at the very next stage.
            self.assertFalse((first_output / "resume-checkpoint.json").exists())
            manifest = json.loads(
                (first_output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "quality_pending")
            self.assertEqual(manifest["stages"][-1]["name"], "visuals")
            self.assertEqual(manifest["stages"][-1]["status"], "blocked")

            # A second attempt that tries to resume from the first run's output
            # must find nothing usable and regenerate planning/script fresh -
            # not silently inherit and repeat the same stuck query.
            second_output = root / "second"
            second_router = _FakeRouter()
            second = CleanV2Pipeline(
                router=second_router,
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            result = second.run(
                brief_path=brief_path,
                approved_sha256=approved,
                output_dir=second_output,
                engine_sha="a" * 40,
                runner_sha="b" * 40,
                max_visuals=2,
                resume_from=first_output,
            )
            self.assertEqual(result["status"], "pass")
            second_manifest = json.loads(
                (second_output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(second_manifest["resumed_stages"], [])
            self.assertNotIn("resume_checkpoint_accepted", second_manifest)
            self.assertEqual(
                [event["stage"] for event in second_router.events],
                ["planning", "script"],
            )

    def test_final_master_block_preserves_completed_work_as_quality_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_blocking_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(RuntimeError, "blocked release"):
                pipeline.run(
                    brief_path=brief_path,
                    approved_sha256=compute_brief_sha256(brief),
                    output_dir=output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )

            self.assertTrue((output / "final.mp4").is_file())
            self.assertTrue((output / "final.json").is_file())
            self.assertTrue((output / "final-master-qc.json").is_file())
            manifest = json.loads(
                (output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "quality_pending")
            self.assertEqual(manifest["quality_pending_stage"], "final_master_qc")
            self.assertEqual(manifest["failure_classification"], "pre-layer")
            self.assertEqual(
                manifest["quality_layers_executed"],
                [TEXT_AUDIT_STAGE, CINEMATIC_STAGE, VISUAL_QA_STAGE, OPENING_STAGE, "final_master_qc"],
            )
            self.assertTrue(all(
                item["status"] == "pass" for item in manifest["stages"][:-1]
            ))
            self.assertEqual(manifest["stages"][-1]["name"], "final_master_qc")
            self.assertEqual(manifest["stages"][-1]["status"], "blocked")


    def test_provider_exhaustion_is_attributed_to_infrastructure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=_InfrastructureRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(RuntimeError, "exhausted bounded provider route"):
                pipeline.run(
                    brief_path=brief_path,
                    approved_sha256=compute_brief_sha256(brief),
                    output_dir=output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )
            manifest = json.loads(
                (output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["failure_classification"], "infrastructure")
            self.assertEqual(manifest["stages"][-1]["name"], "planning")
            self.assertEqual(
                manifest["stages"][-1]["failure_classification"], "infrastructure"
            )


    def test_cinematic_block_is_pre_layer_after_accepted_m7_m11_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_blocking_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(RuntimeError, "new layer block"):
                pipeline.run(
                    brief_path=brief_path,
                    approved_sha256=compute_brief_sha256(brief),
                    output_dir=output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )

            manifest = json.loads(
                (output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "quality_pending")
            self.assertEqual(manifest["quality_pending_stage"], CINEMATIC_STAGE)
            self.assertEqual(manifest["failure_classification"], "pre-layer")
            self.assertEqual(
                manifest["quality_layers_executed"],
                [TEXT_AUDIT_STAGE, CINEMATIC_STAGE, VISUAL_QA_STAGE, OPENING_STAGE],
            )
            self.assertFalse((output / "final.json").exists())
            self.assertFalse((output / "final-master-qc.json").exists())


    def test_opening_content_block_preserves_visuals_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(
                json.dumps(brief, ensure_ascii=False), encoding="utf-8"
            )
            approved = compute_brief_sha256(brief)
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                opening_director=_blocking_opening_director,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(RuntimeError, "CLEAN_V2_OPENING_BLOCK"):
                pipeline.run(
                    brief_path=brief_path,
                    approved_sha256=approved,
                    output_dir=output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )

            manifest = json.loads(
                (output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "quality_pending")
            self.assertEqual(manifest["quality_pending_stage"], OPENING_STAGE)
            self.assertEqual(manifest["failure_classification"], "new-layer-block")
            checkpoint = json.loads(
                (output / "resume-checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["completed_stage"], "visuals")
            self.assertIn("rights-manifest.json", checkpoint["artifacts"])
            self.assertFalse((output / "final.mp4").exists())

    def test_visual_qa_content_block_downgrades_checkpoint_to_voice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            approved = compute_brief_sha256(brief)
            first_output = root / "first"
            first = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_blocking_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(RuntimeError, "CLEAN_V2_VISUAL_QA_BLOCK"):
                first.run(
                    brief_path=brief_path,
                    approved_sha256=approved,
                    output_dir=first_output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )

            manifest = json.loads(
                (first_output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "quality_pending")
            self.assertEqual(manifest["quality_pending_stage"], VISUAL_QA_STAGE)
            self.assertEqual(manifest["failure_classification"], "new-layer-block")
            self.assertEqual(manifest["stages"][-1]["name"], VISUAL_QA_STAGE)
            self.assertFalse((first_output / "final.mp4").exists())

            checkpoint = json.loads(
                (first_output / "resume-checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["completed_stage"], "voice")
            self.assertEqual(
                checkpoint["voice_provider"], "nabra:af_msa"
            )
            self.assertTrue(checkpoint["voice_fallback_used"])
            self.assertNotIn("rights-manifest.json", checkpoint["artifacts"])
            self.assertFalse(
                any(path.startswith("visuals/") for path in checkpoint["artifacts"])
            )

            class _ForbiddenRouter:
                events: list[dict] = []

                def route(self, **_kwargs):
                    raise AssertionError("planning and script must be resumed")

            class _ForbiddenVoice:
                def synthesize(self, *_args, **_kwargs):
                    raise AssertionError("voice must be resumed")

            second_visuals = _FakeVisuals()
            second_output = root / "second"
            second = CleanV2Pipeline(
                router=_ForbiddenRouter(),
                voice_synthesizer=_ForbiddenVoice(),
                visual_source=second_visuals,
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            result = second.run(
                brief_path=brief_path,
                approved_sha256=approved,
                output_dir=second_output,
                engine_sha="a" * 40,
                runner_sha="b" * 40,
                max_visuals=2,
                resume_from=first_output,
            )
            self.assertEqual(result["status"], "pass")
            self.assertEqual(second_visuals.calls, 1)
            second_manifest = json.loads(
                (second_output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                second_manifest["resumed_stages"],
                ["planning", IDENTITY_STAGE, "script", "voice"],
            )
            self.assertTrue(second_manifest["resume_checkpoint_accepted"])
            self.assertEqual(second_manifest["resume_completed_stage"], "voice")

    def test_visual_qa_provider_exhaustion_is_infrastructure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_infrastructure_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(RuntimeError, "CLEAN_V2_VISUAL_QA_INFRASTRUCTURE"):
                pipeline.run(
                    brief_path=brief_path,
                    approved_sha256=compute_brief_sha256(brief),
                    output_dir=output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )
            manifest = json.loads(
                (output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["failure_classification"], "infrastructure")
            self.assertEqual(manifest["stages"][-1]["name"], VISUAL_QA_STAGE)
            checkpoint = json.loads(
                (output / "resume-checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["completed_stage"], "visuals")

    def test_text_audit_block_is_pre_layer_and_stops_before_voice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_blocking_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(RuntimeError, "blocked real production"):
                pipeline.run(
                    brief_path=brief_path,
                    approved_sha256=compute_brief_sha256(brief),
                    output_dir=output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )

            manifest = json.loads(
                (output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "quality_pending")
            self.assertEqual(manifest["quality_pending_stage"], TEXT_AUDIT_STAGE)
            self.assertEqual(manifest["failure_classification"], "pre-layer")
            self.assertEqual(manifest["quality_layers_executed"], [TEXT_AUDIT_STAGE])
            self.assertEqual(manifest["stages"][-1]["name"], TEXT_AUDIT_STAGE)
            self.assertEqual(manifest["stages"][-1]["status"], "blocked")
            self.assertTrue((output / "factuality-audit.json").is_file())
            self.assertFalse((output / "narration.wav").exists())
            self.assertFalse((output / "resume-checkpoint.json").exists())

            # A genuine factuality block makes this exact script unusable. A later
            # attempt must regenerate planning/script instead of inheriting the
            # rejected checkpoint.
            second_output = root / "second"
            second_router = _FakeRouter()
            second = CleanV2Pipeline(
                router=second_router,
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            result = second.run(
                brief_path=brief_path,
                approved_sha256=compute_brief_sha256(brief),
                output_dir=second_output,
                engine_sha="a" * 40,
                runner_sha="b" * 40,
                max_visuals=2,
                resume_from=output,
            )
            self.assertEqual(result["status"], "pass")
            second_manifest = json.loads(
                (second_output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(second_manifest["resumed_stages"], [])
            self.assertNotIn("resume_checkpoint_accepted", second_manifest)
            self.assertEqual(
                [event["stage"] for event in second_router.events],
                ["planning", "script"],
            )

    def test_text_audit_provider_exhaustion_is_infrastructure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_infrastructure_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(RuntimeError, "exhausted bounded provider route"):
                pipeline.run(
                    brief_path=brief_path,
                    approved_sha256=compute_brief_sha256(brief),
                    output_dir=output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )
            manifest = json.loads(
                (output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["failure_classification"], "infrastructure")
            self.assertEqual(manifest["stages"][-1]["name"], TEXT_AUDIT_STAGE)
            checkpoint = json.loads(
                (output / "resume-checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["completed_stage"], "script")

    def test_audio_mastering_failure_is_a_plain_technical_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_failing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(RuntimeError, "audio_loudness_measurement_unparseable"):
                pipeline.run(
                    brief_path=brief_path,
                    approved_sha256=compute_brief_sha256(brief),
                    output_dir=output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )
            manifest = json.loads(
                (output / "run-manifest.json").read_text(encoding="utf-8")
            )
            # Audio mastering is a transform, not a content gate: any failure is a
            # plain technical failure, never "quality_pending".
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["failure_classification"], "pre-layer")
            self.assertEqual(manifest["stages"][-1]["name"], AUDIO_MASTERING_STAGE)
            self.assertEqual(manifest["stages"][-1]["status"], "failed")
            self.assertFalse((output / "narration-mastered.wav").exists())

    def test_narrative_identity_provider_exhaustion_is_infrastructure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_infrastructure_narrative_identity,
            )
            with self.assertRaisesRegex(RuntimeError, "exhausted bounded provider route"):
                pipeline.run(
                    brief_path=brief_path,
                    approved_sha256=compute_brief_sha256(brief),
                    output_dir=output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )
            manifest = json.loads(
                (output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["failure_classification"], "infrastructure")
            self.assertEqual(manifest["stages"][-1]["name"], IDENTITY_STAGE)
            self.assertEqual(manifest["stages"][-1]["status"], "failed")
            self.assertFalse((output / "narrative-identity.json").exists())
            self.assertFalse((output / "script.json").exists())

    def test_narrative_identity_splices_opener_and_closer_into_final_script(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            result = pipeline.run(
                brief_path=brief_path,
                approved_sha256=compute_brief_sha256(brief),
                output_dir=output,
                engine_sha="a" * 40,
                runner_sha="b" * 40,
                max_visuals=2,
            )
            self.assertEqual(result["status"], "pass")

            identity = json.loads(
                (output / "narrative-identity.json").read_text(encoding="utf-8")
            )
            script = json.loads((output / "script.json").read_text(encoding="utf-8"))
            sections = script["sections"]
            joined = "\n".join(item["narration"] for item in sections)
            self.assertEqual(joined.count(identity["opener"]), 1)
            self.assertEqual(joined.count(identity["closer"]), 1)
            self.assertIn(identity["opener"], sections[0]["narration"])
            self.assertNotIn(identity["opener"], sections[-1]["narration"])
            self.assertTrue(
                sections[-1]["narration"].rstrip().endswith(identity["closer"])
            )
            for section in sections[1:-1]:
                self.assertNotIn(identity["opener"], section["narration"])
                self.assertNotIn(identity["closer"], section["narration"])

            transcript = (output / "narration.txt").read_text(encoding="utf-8")
            self.assertIn(identity["opener"], transcript)
            self.assertIn(identity["closer"], transcript)

    def test_narrative_identity_is_resumed_together_with_script(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            approved = compute_brief_sha256(brief)
            first_output = root / "first"
            first = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_infrastructure_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(
                RuntimeError, "CLEAN_V2_VISUAL_QA_INFRASTRUCTURE"
            ):
                first.run(
                    brief_path=brief_path,
                    approved_sha256=approved,
                    output_dir=first_output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )
            first_identity = json.loads(
                (first_output / "narrative-identity.json").read_text(encoding="utf-8")
            )

            def _forbidden_narrative_identity(**_kwargs):
                raise AssertionError(
                    "narrative identity must be resumed, not regenerated"
                )

            second_output = root / "second"
            second = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_passing_visual_qa,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_forbidden_narrative_identity,
            )
            result = second.run(
                brief_path=brief_path,
                approved_sha256=approved,
                output_dir=second_output,
                engine_sha="a" * 40,
                runner_sha="b" * 40,
                max_visuals=2,
                resume_from=first_output,
            )
            self.assertEqual(result["status"], "pass")
            second_identity = json.loads(
                (second_output / "narrative-identity.json").read_text(encoding="utf-8")
            )
            self.assertEqual(second_identity, first_identity)
            manifest = json.loads(
                (second_output / "run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["resumed_stages"],
                ["planning", IDENTITY_STAGE, "script", "voice", "visuals"],
            )
            resumed_by_name = {
                stage["name"]: stage.get("resumed") for stage in manifest["stages"]
            }
            self.assertTrue(resumed_by_name[IDENTITY_STAGE])
            self.assertTrue(resumed_by_name["script"])


class VisualQADiagnosticsTests(unittest.TestCase):
    def test_contract_error_preserves_raw_provider_http_evidence_before_wrapping(self) -> None:
        source = Path("clean_v2/visual_qa.py").read_text(encoding="utf-8")
        diagnostic = source.index("visual-qa-diagnostics.json")
        wrapped = source.index("reason=visual_audit_contract_error")
        self.assertLess(diagnostic, wrapped)
        for field in (
            '"error_code"',
            '"provider"',
            '"requested_model"',
            '"resolved_model"',
            '"http_status"',
            '"http_message"',
            '"detail"',
        ):
            self.assertIn(field, source)


class StockVisualRecoveryPoolTests(unittest.TestCase):
    @staticmethod
    def _candidate(provider: str, asset_id: str) -> dict:
        return {
            "provider": provider,
            "asset_id": asset_id,
            "download_url": f"https://media.invalid/{provider}/{asset_id}.mp4",
            "source_url": f"https://source.invalid/{provider}/{asset_id}",
            "creator": "test",
            "creator_url": "",
            "query": "person checking calendar at desk",
        }

    def test_recovery_pool_interleaves_provider_rank_and_caps_at_three(self) -> None:
        source = media_module.StockVisualSource()
        pexels = [
            self._candidate("pexels", "p1"),
            self._candidate("pexels", "p2"),
            self._candidate("pexels", "p3"),
        ]
        pixabay = [
            self._candidate("pixabay", "x1"),
            self._candidate("pixabay", "x2"),
            self._candidate("pixabay", "x3"),
        ]

        def fake_download(_url, destination):
            Path(destination).write_bytes(b"V" * 4096)

        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            source,
            "_pexels_recovery_pool",
            return_value=pexels,
        ) as pexels_search, mock.patch.object(
            source,
            "_pixabay_recovery_pool",
            return_value=pixabay,
        ) as pixabay_search, mock.patch.object(
            media_module,
            "_download_media",
            side_effect=fake_download,
        ):
            output = Path(root)
            result = source.acquire_replacement_candidates(
                "person checking calendar at desk",
                output,
                "film",
                destination_name="visual-01.mp4",
                section_id="s1",
                max_candidates=3,
            )

        self.assertEqual(pexels_search.call_count, 1)
        self.assertEqual(pixabay_search.call_count, 1)
        self.assertEqual(
            [
                (row["provider"], row["asset_id"])
                for _path, row in result
            ],
            [("pexels", "p1"), ("pixabay", "x1"), ("pexels", "p2")],
        )
        self.assertEqual(
            [row["semantic_recovery_candidate_index"] for _path, row in result],
            [1, 2, 3],
        )

    def test_recovery_candidate_limit_is_hard_capped_at_three(self) -> None:
        source = media_module.StockVisualSource()
        pexels = [
            self._candidate("pexels", f"p{index}")
            for index in range(1, 7)
        ]
        pixabay = [
            self._candidate("pixabay", f"x{index}")
            for index in range(1, 7)
        ]

        def fake_download(_url, destination):
            Path(destination).write_bytes(b"V" * 4096)

        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            source,
            "_pexels_recovery_pool",
            return_value=pexels,
        ), mock.patch.object(
            source,
            "_pixabay_recovery_pool",
            return_value=pixabay,
        ), mock.patch.object(
            media_module,
            "_download_media",
            side_effect=fake_download,
        ):
            result = source.acquire_replacement_candidates(
                "person checking calendar at desk",
                Path(root),
                "film",
                destination_name="visual-01.mp4",
                section_id="s1",
                max_candidates=99,
            )

        self.assertEqual(len(result), 3)


class VisualQASemanticRecoveryTests(unittest.TestCase):
    ORIGINAL_QUERY = "person scrolling phone while looking at wall clock"
    ALTERNATE_QUERY = "person avoiding open laptop task while scrolling phone beside clock"
    NARRATION = (
        "العامل الثاني هو المماطلة، التي تُعتبر فشلًا في التنظيم الذاتي. "
        "لا تُعزى إلى الكسل أو ضعف الشخصية، بل هي نتيجة لتجنب المهام التي تبدو غير ممتعة، "
        "أو شعورنا بأننا غير كفوّين في إتمامها، أو رغبتنا في الحصول على شعور عاطفي إيجابي "
        "على المدى القصير. هذه العوامل تجعلنا نؤجل العمل، حتى وإن كنا نعرف أن المهمة ضرورية."
    )

    class _Router:
        def __init__(self, alternate: str) -> None:
            self.alternate = alternate
            self.calls = 0
            self.events: list[dict] = []

        def route(self, *, stage, prompt, max_tokens, validator):
            self.calls += 1
            if stage != "visual_query_recovery":
                raise AssertionError(stage)
            if "المماطلة" not in prompt or "تجنب المهام" not in prompt:
                raise AssertionError("alternate query prompt lost the actual s3 narration")
            self.events.append(
                {
                    "stage": stage,
                    "provider": "test-provider",
                    "result": "success",
                    "wire_attempted": True,
                }
            )
            return validator({"alternate_query": self.alternate})

    class _FailingRecoveryRouter:
        def __init__(self) -> None:
            self.calls = 0
            self.events = [
                {
                    "stage": "visual_query_recovery",
                    "provider": "gemini",
                    "result": "http_429",
                    "wire_attempted": True,
                },
                {
                    "stage": "visual_query_recovery",
                    "provider": "groq",
                    "result": "http_400",
                    "wire_attempted": True,
                },
                {
                    "stage": "visual_query_recovery",
                    "provider": "openrouter",
                    "result": "http_429",
                    "wire_attempted": True,
                },
            ]

        def route(self, *, stage, prompt, max_tokens, validator):
            del prompt, max_tokens, validator
            self.calls += 1
            if stage != "visual_query_recovery":
                raise AssertionError(stage)
            raise RuntimeError(
                "visual_query_recovery exhausted bounded provider route: "
                "gemini:http_429, groq:http_400, openrouter:http_429"
            )

    class _VisualSource:
        def __init__(self, candidate_count: int = 1) -> None:
            self.acquire_calls = 0
            self.commit_calls = 0
            self.events: list[dict] = []
            self.candidate_count = candidate_count

        @staticmethod
        def _assert_request(
            query,
            *,
            destination_name,
            section_id,
            exclude_provider,
            exclude_asset_id,
            exclude_assets,
        ) -> None:
            if query != VisualQASemanticRecoveryTests.ALTERNATE_QUERY:
                raise AssertionError(query)
            if section_id != "s3" or destination_name != "visual-03.mp4":
                raise AssertionError((section_id, destination_name))
            if exclude_provider != "pexels" or str(exclude_asset_id) != "6943542":
                raise AssertionError((exclude_provider, exclude_asset_id))
            if ("pexels", "6943542") not in [
                (str(provider), str(asset_id)) for provider, asset_id in exclude_assets
            ]:
                raise AssertionError(exclude_assets)

        @staticmethod
        def _candidate(output_dir, *, query, destination_name, section_id, index):
            provider = "pexels" if index % 2 else "pixabay"
            asset_id = f"999000{index}"
            path = (
                Path(output_dir)
                / f".visual-03.semantic-recovery-{index:02d}-{provider}.mp4"
            )
            path.write_bytes(b"R" * 4096)
            return path, {
                "provider": provider,
                "asset_id": asset_id,
                "source_url": f"https://example.invalid/{provider}/{asset_id}",
                "creator": "test",
                "creator_url": "https://example.invalid/test",
                "query": query,
                "local_file": destination_name,
                "section_id": section_id,
                "semantic_recovery": True,
                "semantic_recovery_candidate_index": index,
            }

        def acquire_replacement_candidates(
            self,
            query,
            output_dir,
            fmt,
            *,
            destination_name,
            section_id,
            max_candidates,
            exclude_provider,
            exclude_asset_id,
            exclude_assets,
        ):
            del fmt
            self.acquire_calls += 1
            self._assert_request(
                query,
                destination_name=destination_name,
                section_id=section_id,
                exclude_provider=exclude_provider,
                exclude_asset_id=exclude_asset_id,
                exclude_assets=exclude_assets,
            )
            if max_candidates != 3:
                raise AssertionError(max_candidates)
            return [
                self._candidate(
                    output_dir,
                    query=query,
                    destination_name=destination_name,
                    section_id=section_id,
                    index=index,
                )
                for index in range(
                    1,
                    min(self.candidate_count, max_candidates) + 1,
                )
            ]

        def acquire_replacement(
            self,
            query,
            output_dir,
            fmt,
            *,
            destination_name,
            section_id,
            exclude_provider,
            exclude_asset_id,
            exclude_assets,
        ):
            del fmt
            self.acquire_calls += 1
            self._assert_request(
                query,
                destination_name=destination_name,
                section_id=section_id,
                exclude_provider=exclude_provider,
                exclude_asset_id=exclude_asset_id,
                exclude_assets=exclude_assets,
            )
            return self._candidate(
                output_dir,
                query=query,
                destination_name=destination_name,
                section_id=section_id,
                index=1,
            )

        def commit_replacement(self, replacement, destination):
            self.commit_calls += 1
            os.replace(replacement, destination)
            return Path(destination)

    @staticmethod
    def _audit(*, relevance: float, quality: float, status: str, evidence) -> dict:
        return {
            "status": status,
            "relevance": relevance,
            "visual_quality": quality,
            "identifiable_person": False,
            "sensitive_trait_implication_risk": False,
            "prominent_logo_or_brand": False,
            "cultural_conflict": False,
            "cultural_islamic_suitability_risk": False,
            "advertiser_conflict": False,
            "obvious_synthetic_or_visual_artifact": False,
            "reason": "controlled s3 semantic recovery reproduction",
            "vision_provider": "mistral",
            "resolved_model": "ministral-14b-2512",
            "prompt_hash": evidence.prompt_hash,
            "frame_sha256": list(evidence.frame_sha256),
        }

    def _run_case(
        self,
        *,
        recovery_relevance: float | None = None,
        recovery_relevances: list[float] | None = None,
    ):
        scores = list(
            recovery_relevances
            if recovery_relevances is not None
            else [float(recovery_relevance if recovery_relevance is not None else 0.0)]
        )
        router = self._Router(self.ALTERNATE_QUERY)
        visual_source = self._VisualSource(candidate_count=len(scores))
        plan = {
            "sections": [
                {
                    "id": "s3",
                    "heading": "المماطلة كفشل تنظيم ذاتي",
                    "purpose": (
                        "يُظهر أن المماطلة ناتجة عن تجنب المهام، ضعف الثقة في المهارة، "
                        "والبحث عن الراحة العاطفية"
                    ),
                    "visual_query_en": self.ORIGINAL_QUERY,
                }
            ]
        }
        visual_story = {
            "schema_version": 1,
            "visual_world": (
                "Grounded hopeful cinematic realism, soft natural light, warm neutral colors, "
                "objects and environments, no identifiable faces."
            ),
            "story_arc": {
                "beginning": "show the friction clearly",
                "transformation": "make the avoidance mechanism visible",
                "arrival": "land on a practical shift",
            },
            "beats": [
                {
                    "id": "b1",
                    "section_id": "s3",
                    "viewer_intent": "understand the avoidance mechanism",
                    "shot_intent": self.ORIGINAL_QUERY,
                    "source_preference": "stock_motion",
                }
            ],
        }
        script = {"sections": [{"id": "s3", "narration": self.NARRATION}]}
        rights = [
            {
                "provider": "pexels",
                "asset_id": "6943542",
                "source_url": (
                    "https://www.pexels.com/video/"
                    "man-in-bed-looking-at-phone-and-alarm-clock-6943542/"
                ),
                "creator": "cottonbro studio",
                "creator_url": "https://www.pexels.com/@cottonbro",
                "query": self.ORIGINAL_QUERY,
                "local_file": "visual-03.mp4",
                "section_id": "s3",
                "beat_id": "b1",
                "viewer_intent": "understand the avoidance mechanism",
                "shot_intent": self.ORIGINAL_QUERY,
                "source_preference": "stock_motion",
                "source_actual": "stock_motion",
            }
        ]

        evidence_counter = {"n": 0}
        audit_counter = {"n": 0}
        intended_visual_calls: list[str] = []

        def build_evidence(_clip, _bundle, **_kwargs):
            intended_visual_calls.append(str(_kwargs.get("intended_visual") or ""))
            evidence_counter["n"] += 1
            n = evidence_counter["n"]
            return SimpleNamespace(
                prompt_hash=f"prompt-{n}",
                frame_sha256=(f"frame-{n}-1", f"frame-{n}-2", f"frame-{n}-3"),
            )

        def ledger_call(_ledger, _spec, *_args, **kwargs):
            audit_counter["n"] += 1
            evidence = kwargs["canonical_evidence"]
            if audit_counter["n"] == 1:
                return self._audit(
                    relevance=0.40,
                    quality=0.95,
                    status="block",
                    evidence=evidence,
                )
            score_index = min(audit_counter["n"] - 2, len(scores) - 1)
            score = scores[score_index]
            return self._audit(
                relevance=score,
                quality=0.95,
                status="pass",
                evidence=evidence,
            )

        with tempfile.TemporaryDirectory() as root:
            output = Path(root)
            visuals = output / "visuals"
            visuals.mkdir()
            original = visuals / "visual-03.mp4"
            original.write_bytes(b"O" * 4096)
            (output / "rights-manifest.json").write_text(
                json.dumps({"schema_version": 1, "assets": rights}),
                encoding="utf-8",
            )
            (output / "visual-story.json").write_text(
                json.dumps(visual_story),
                encoding="utf-8",
            )

            class FakeBudgetLedger:
                def __init__(self, _fmt, *, enforce=True):
                    self.enforce = enforce

                def write(self, path):
                    Path(path).write_text(
                        json.dumps({"schema_version": 1, "provider_attempts": {}}),
                        encoding="utf-8",
                    )

                def to_summary(self):
                    return {"provider_attempts": {}}

            class FakeTaskSpec:
                def __init__(self, **kwargs):
                    self.__dict__.update(kwargs)

            class FakeVisionStageError(RuntimeError):
                def __init__(self, message="vision error"):
                    super().__init__(message)
                    self.code = SimpleNamespace(value="PROVIDER_TRANSIENT")
                    self.provider = "fake"
                    self.requested_model = "fake"
                    self.resolved_model = "fake"
                    self.http_status = 429
                    self.http_message = "fake"
                    self.detail = "fake"

            engine = types.ModuleType("isco_video_agent")
            engine.__path__ = []
            ai_budget = types.ModuleType("isco_video_agent.ai_budget")
            ai_budget.BudgetLedger = FakeBudgetLedger
            ai_budget.Capability = SimpleNamespace(VISION="vision")
            ai_budget.Priority = SimpleNamespace(P0="P0")
            ai_budget.TaskSpec = FakeTaskSpec
            orchestrator = types.ModuleType("isco_video_agent.orchestrator")
            orchestrator._ledger_call_status = ledger_call
            visual_selection = types.ModuleType("isco_video_agent.visual_selection")
            visual_selection.FINAL_CUT_TARGET_SEMANTIC_FLOOR = 0.85
            visual_selection.semantic_floor = lambda audit: min(
                float(audit.get("relevance", 0.0) or 0.0),
                float(audit.get("visual_quality", 0.0) or 0.0),
            )
            visual_selection.is_final_cut_ready = lambda audit: (
                str(audit.get("status") or "").lower() == "pass"
                and visual_selection.semantic_floor(audit) >= 0.85
            )

            canonical = types.ModuleType("scripts.canonical_visual_evidence_v1")
            canonical.build_canonical_visual_evidence = build_evidence
            canonical.audit_gemini_canonical_evidence = lambda *_a, **_k: None
            mesh = types.ModuleType("scripts.run181_vision_mesh_closure")
            mesh.install_run181_vision_mesh_closure = lambda: None
            reliability = types.ModuleType("scripts.vision_provider_reliability")
            reliability.vision_provider_circuit_scope = lambda: contextlib.nullcontext()
            reliability.VisionProviderMeshUnavailableError = type(
                "VisionProviderMeshUnavailableError", (RuntimeError,), {}
            )
            mistral_visual = types.ModuleType("scripts.mistral_visual_qa_fallback")
            mistral_visual.reset_mistral_visual_qa_telemetry = lambda: None
            mistral_visual.get_mistral_visual_qa_telemetry = lambda: []
            contract = types.ModuleType("scripts.vision_stage_contract_v2")
            contract.VisionStageError = FakeVisionStageError
            contract.install_vision_provider_reliability = lambda: None

            fake_modules = {
                "isco_video_agent": engine,
                "isco_video_agent.ai_budget": ai_budget,
                "isco_video_agent.orchestrator": orchestrator,
                "isco_video_agent.visual_selection": visual_selection,
                "scripts.canonical_visual_evidence_v1": canonical,
                "scripts.run181_vision_mesh_closure": mesh,
                "scripts.vision_provider_reliability": reliability,
                "scripts.mistral_visual_qa_fallback": mistral_visual,
                "scripts.vision_stage_contract_v2": contract,
            }
            with mock.patch.dict(sys.modules, fake_modules), mock.patch.dict(
                os.environ,
                {"GEMINI_API_KEY": "test-key", "GEMINI_CONTENT_MODEL": "gemini-3.7-flash"},
                clear=False,
            ):
                if any(score >= 0.85 for score in scores):
                    result = visual_qa_module.run_final_cut_visual_qa(
                        output_dir=output,
                        plan=plan,
                        script=script,
                        rights=rights,
                        fmt="film",
                        router=router,
                        visual_source=visual_source,
                    )
                    error = None
                else:
                    result = None
                    with self.assertRaisesRegex(
                        visual_qa_module.CleanV2VisualQABlock,
                        "semantic_recovery_not_final_cut_ready",
                    ) as raised:
                        visual_qa_module.run_final_cut_visual_qa(
                            output_dir=output,
                            plan=plan,
                            script=script,
                            rights=rights,
                            fmt="film",
                            router=router,
                            visual_source=visual_source,
                        )
                    error = str(raised.exception)

            audits = json.loads((output / "visual-audit.json").read_text(encoding="utf-8"))
            recovery = json.loads(
                (output / "visual-query-recovery.json").read_text(encoding="utf-8")
            )
            manifest = json.loads(
                (output / "rights-manifest.json").read_text(encoding="utf-8")
            )
            final_bytes = original.read_bytes()
            return {
                "result": result,
                "error": error,
                "audits": audits,
                "recovery": recovery,
                "manifest": manifest,
                "rights": rights,
                "final_bytes": final_bytes,
                "router_calls": router.calls,
                "acquire_calls": visual_source.acquire_calls,
                "commit_calls": visual_source.commit_calls,
                "audit_calls": audit_counter["n"],
                "intended_visual_calls": intended_visual_calls,
            }

    def test_attempt1_s3_floor_040_gets_one_narration_bound_recovery_and_passes(self) -> None:
        outcome = self._run_case(recovery_relevance=0.92)

        self.assertEqual(outcome["router_calls"], 1)
        self.assertEqual(outcome["acquire_calls"], 1)
        self.assertEqual(outcome["commit_calls"], 1)
        self.assertEqual(outcome["audit_calls"], 2)
        self.assertEqual(outcome["result"]["status"], "pass")
        self.assertEqual(outcome["result"]["final_cut_readiness_target"], 0.85)
        self.assertEqual(outcome["result"]["semantic_recovery_count"], 1)
        self.assertTrue(outcome["result"]["final_media_mutated"])
        self.assertEqual(outcome["audits"][0]["final_cut_semantic_floor"], 0.4)
        self.assertFalse(outcome["audits"][0]["is_selected"])
        self.assertEqual(outcome["audits"][1]["final_cut_semantic_floor"], 0.92)
        self.assertTrue(outcome["audits"][1]["is_selected"])
        self.assertEqual(outcome["recovery"][0]["attempt_limit"], 1)
        self.assertEqual(outcome["recovery"][0]["status"], "recovered")
        self.assertEqual(outcome["rights"][0]["asset_id"], "9990001")
        self.assertEqual(outcome["rights"][0]["recovery_of_asset_id"], "6943542")
        self.assertEqual(outcome["manifest"]["assets"][0]["query"], self.ALTERNATE_QUERY)
        self.assertEqual(outcome["final_bytes"], b"R" * 4096)

    def test_recovery_is_strictly_one_shot_when_second_clip_still_below_085(self) -> None:
        outcome = self._run_case(recovery_relevance=0.70)

        self.assertEqual(outcome["router_calls"], 1)
        self.assertEqual(outcome["acquire_calls"], 1)
        self.assertEqual(outcome["commit_calls"], 0)
        self.assertEqual(outcome["audit_calls"], 2)
        self.assertIn("primary_floor=0.400000", outcome["error"])
        self.assertIn("recovery_floor=0.700000", outcome["error"])
        self.assertEqual(outcome["recovery"][0]["status"], "rejected")
        self.assertEqual(outcome["rights"][0]["asset_id"], "6943542")
        self.assertEqual(outcome["final_bytes"], b"O" * 4096)


    def test_three_candidates_are_context_reviewed_before_best_is_selected(self) -> None:
        # Candidate 1 already clears the 0.85 readiness floor. Phase B must
        # still review candidates 2 and 3, then choose the strongest contextual
        # fit instead of accepting the first passing candidate immediately.
        outcome = self._run_case(
            recovery_relevances=[0.90, 0.92, 0.95],
        )

        self.assertEqual(outcome["router_calls"], 1)
        self.assertEqual(outcome["acquire_calls"], 1)
        self.assertEqual(outcome["commit_calls"], 1)
        self.assertEqual(outcome["audit_calls"], 4)
        self.assertEqual(len(outcome["intended_visual_calls"]), 4)
        for contextual_visual in outcome["intended_visual_calls"]:
            self.assertIn("Current:", contextual_visual)
            self.assertIn("Previous: story opening", contextual_visual)
            self.assertIn("Next: story arrival", contextual_visual)
            self.assertIn("Same hook-to-payoff arc:", contextual_visual)
        self.assertEqual(outcome["result"]["status"], "pass")
        self.assertEqual(
            outcome["result"]["semantic_recovery_candidate_review_limit_per_section"],
            3,
        )
        self.assertEqual(
            outcome["result"]["candidate_selection_mode"],
            "review_all_ready_candidates_then_best_contextual_floor",
        )
        record = outcome["recovery"][0]
        self.assertEqual(record["candidate_pool_size"], 3)
        self.assertEqual(record["candidate_review_count"], 3)
        self.assertEqual(record["selected_candidate_index"], 3)
        self.assertEqual(
            [item["status"] for item in record["candidate_reviews"]],
            ["ready", "ready", "ready"],
        )
        self.assertEqual(outcome["rights"][0]["provider"], "pexels")
        self.assertEqual(outcome["rights"][0]["asset_id"], "9990003")

    def test_three_recovery_candidates_is_hard_fail_closed_limit(self) -> None:
        outcome = self._run_case(
            recovery_relevances=[0.60, 0.70, 0.80],
        )

        self.assertEqual(outcome["router_calls"], 1)
        self.assertEqual(outcome["acquire_calls"], 1)
        self.assertEqual(outcome["commit_calls"], 0)
        self.assertEqual(outcome["audit_calls"], 4)
        self.assertIn("recovery_floor=0.800000", outcome["error"])
        self.assertIn("reviewed=3", outcome["error"])
        record = outcome["recovery"][0]
        self.assertEqual(record["candidate_pool_size"], 3)
        self.assertEqual(record["candidate_review_count"], 3)
        self.assertEqual(
            [item["status"] for item in record["candidate_reviews"]],
            ["rejected", "rejected", "rejected"],
        )
        self.assertEqual(outcome["rights"][0]["asset_id"], "6943542")
        self.assertEqual(outcome["final_bytes"], b"O" * 4096)

    def test_attempt1_recovery_provider_exhaustion_is_infrastructure_not_content(self) -> None:
        original_router = self._Router
        self._Router = lambda _alternate: self._FailingRecoveryRouter()
        try:
            with self.assertRaises(
                visual_qa_module.CleanV2VisualQAInfrastructure
            ) as raised:
                self._run_case(recovery_relevance=0.92)
        finally:
            self._Router = original_router
        self.assertIn(
            "reason=semantic_recovery_query_unavailable",
            str(raised.exception),
        )
        self.assertIn(
            "CLEAN_V2_VISUAL_QA_INFRASTRUCTURE",
            str(raised.exception),
        )


class VisualQAMultiClipSectionTests(unittest.TestCase):
    """Test Requirement F: a section with a primary plus pacing_auxiliary
    clips reviews every one of them independently, a failing auxiliary's
    bounded recovery targets only that clip's own slot (never the primary
    or a sibling auxiliary), and the final report counts every clip that
    was actually audited, not one per section."""

    NARRATION = (
        "نص سردي قصير لهذا القسم يوضح الفكرة الأساسية بإيجاز شديد لغرض هذا الاختبار."
    )

    @staticmethod
    def _audit(*, relevance: float, quality: float, status: str, evidence) -> dict:
        return {
            "status": status,
            "relevance": relevance,
            "visual_quality": quality,
            "identifiable_person": False,
            "sensitive_trait_implication_risk": False,
            "prominent_logo_or_brand": False,
            "cultural_conflict": False,
            "cultural_islamic_suitability_risk": False,
            "advertiser_conflict": False,
            "obvious_synthetic_or_visual_artifact": False,
            "reason": "multi-clip section reproduction",
            "vision_provider": "mistral",
            "resolved_model": "ministral-14b-2512",
            "prompt_hash": evidence.prompt_hash,
            "frame_sha256": list(evidence.frame_sha256),
        }

    class _Router:
        def __init__(self) -> None:
            self.calls = 0

        def route(self, *, stage, prompt, max_tokens, validator):
            del prompt, max_tokens
            self.calls += 1
            if stage != "visual_query_recovery":
                raise AssertionError(stage)
            return validator({"alternate_query": "a different four word phrase"})

    class _VisualSource:
        def __init__(self) -> None:
            self.acquire_calls = 0
            self.commit_calls = 0
            self.committed_destinations: list[str] = []
            self.requested_destination_names: list[str] = []

        def acquire_replacement_candidates(
            self,
            query,
            output_dir,
            fmt,
            *,
            destination_name,
            section_id,
            max_candidates,
            exclude_provider,
            exclude_asset_id,
            exclude_assets,
        ):
            del fmt, max_candidates, exclude_provider, exclude_asset_id, exclude_assets
            self.acquire_calls += 1
            self.requested_destination_names.append(destination_name)
            path = (
                Path(output_dir)
                / f".{destination_name}.semantic-recovery-01-pexels.mp4"
            )
            path.write_bytes(b"R" * 4096)
            row = {
                "provider": "pexels",
                "asset_id": "recovered-1",
                "source_url": "https://example.invalid/pexels/recovered-1",
                "creator": "test",
                "creator_url": "",
                "query": query,
                "local_file": destination_name,
                "section_id": section_id,
                "semantic_recovery": True,
                "semantic_recovery_candidate_index": 1,
            }
            return [(path, row)]

        def commit_replacement(self, replacement, destination):
            self.commit_calls += 1
            self.committed_destinations.append(Path(destination).name)
            os.replace(replacement, destination)
            return Path(destination)

    def test_three_clips_in_one_section_all_audited_failing_auxiliary_recovers_in_place(
        self,
    ) -> None:
        plan = {
            "sections": [
                {
                    "id": "s1",
                    "heading": "h",
                    "purpose": "p",
                    "visual_query_en": "quiet desk notebook wide shot",
                }
            ]
        }
        script = {"sections": [{"id": "s1", "narration": self.NARRATION}]}
        rights = [
            {
                "provider": "pexels",
                "asset_id": "primary-1",
                "source_url": "u",
                "creator": "c",
                "creator_url": "",
                "query": "q",
                "local_file": "visual-01.mp4",
                "section_id": "s1",
            },
            {
                "provider": "pexels",
                "asset_id": "aux-1",
                "source_url": "u",
                "creator": "c",
                "creator_url": "",
                "query": "q",
                "local_file": "visual-02.mp4",
                "section_id": "s1",
                "pacing_auxiliary": True,
            },
            {
                "provider": "pexels",
                "asset_id": "aux-2",
                "source_url": "u",
                "creator": "c",
                "creator_url": "",
                "query": "q",
                "local_file": "visual-03.mp4",
                "section_id": "s1",
                "pacing_auxiliary": True,
            },
        ]

        evidence_counter = {"n": 0}

        def build_evidence(_clip, _bundle, **_kwargs):
            evidence_counter["n"] += 1
            n = evidence_counter["n"]
            return SimpleNamespace(
                prompt_hash=f"prompt-{n}",
                frame_sha256=(f"frame-{n}-1", f"frame-{n}-2"),
            )

        # Position 1 (primary) and position 2 (aux #1) pass immediately;
        # position 3 (aux #2) fails first, then its own bounded recovery
        # candidate passes.
        call_outcomes = [
            (0.95, 0.95, "pass"),
            (0.95, 0.95, "pass"),
            (0.30, 0.95, "block"),
            (0.95, 0.95, "pass"),
        ]
        seen: list[int] = []

        def ledger_call(_ledger, _spec, *_args, **kwargs):
            evidence = kwargs["canonical_evidence"]
            index = len(seen)
            seen.append(index)
            relevance, quality, status = call_outcomes[index]
            return self._audit(
                relevance=relevance, quality=quality, status=status, evidence=evidence
            )

        router = self._Router()
        visual_source = self._VisualSource()

        with tempfile.TemporaryDirectory() as root:
            output = Path(root)
            visuals = output / "visuals"
            visuals.mkdir()
            for row in rights:
                (visuals / row["local_file"]).write_bytes(b"O" * 4096)
            (output / "rights-manifest.json").write_text(
                json.dumps({"schema_version": 1, "assets": rights}),
                encoding="utf-8",
            )

            class FakeBudgetLedger:
                def __init__(self, _fmt, *, enforce=True):
                    self.enforce = enforce

                def write(self, path):
                    Path(path).write_text(
                        json.dumps({"schema_version": 1, "provider_attempts": {}}),
                        encoding="utf-8",
                    )

                def to_summary(self):
                    return {"provider_attempts": {}}

            class FakeTaskSpec:
                def __init__(self, **kwargs):
                    self.__dict__.update(kwargs)

            engine = types.ModuleType("isco_video_agent")
            engine.__path__ = []
            ai_budget = types.ModuleType("isco_video_agent.ai_budget")
            ai_budget.BudgetLedger = FakeBudgetLedger
            ai_budget.Capability = SimpleNamespace(VISION="vision")
            ai_budget.Priority = SimpleNamespace(P0="P0")
            ai_budget.TaskSpec = FakeTaskSpec
            orchestrator = types.ModuleType("isco_video_agent.orchestrator")
            orchestrator._ledger_call_status = ledger_call
            visual_selection = types.ModuleType("isco_video_agent.visual_selection")
            visual_selection.FINAL_CUT_TARGET_SEMANTIC_FLOOR = 0.85
            visual_selection.semantic_floor = lambda audit: min(
                float(audit.get("relevance", 0.0) or 0.0),
                float(audit.get("visual_quality", 0.0) or 0.0),
            )
            visual_selection.is_final_cut_ready = lambda audit: (
                str(audit.get("status") or "").lower() == "pass"
                and visual_selection.semantic_floor(audit) >= 0.85
            )

            canonical = types.ModuleType("scripts.canonical_visual_evidence_v1")
            canonical.build_canonical_visual_evidence = build_evidence
            canonical.audit_gemini_canonical_evidence = lambda *_a, **_k: None
            mesh = types.ModuleType("scripts.run181_vision_mesh_closure")
            mesh.install_run181_vision_mesh_closure = lambda: None
            reliability = types.ModuleType("scripts.vision_provider_reliability")
            reliability.vision_provider_circuit_scope = (
                lambda: contextlib.nullcontext()
            )
            reliability.VisionProviderMeshUnavailableError = type(
                "VisionProviderMeshUnavailableError", (RuntimeError,), {}
            )
            mistral_visual = types.ModuleType("scripts.mistral_visual_qa_fallback")
            mistral_visual.reset_mistral_visual_qa_telemetry = lambda: None
            mistral_visual.get_mistral_visual_qa_telemetry = lambda: []
            contract = types.ModuleType("scripts.vision_stage_contract_v2")

            class FakeVisionStageError(RuntimeError):
                pass

            contract.VisionStageError = FakeVisionStageError
            contract.install_vision_provider_reliability = lambda: None

            fake_modules = {
                "isco_video_agent": engine,
                "isco_video_agent.ai_budget": ai_budget,
                "isco_video_agent.orchestrator": orchestrator,
                "isco_video_agent.visual_selection": visual_selection,
                "scripts.canonical_visual_evidence_v1": canonical,
                "scripts.run181_vision_mesh_closure": mesh,
                "scripts.vision_provider_reliability": reliability,
                "scripts.mistral_visual_qa_fallback": mistral_visual,
                "scripts.vision_stage_contract_v2": contract,
            }
            with mock.patch.dict(sys.modules, fake_modules), mock.patch.dict(
                os.environ,
                {"GEMINI_API_KEY": "test-key", "GEMINI_CONTENT_MODEL": "gemini-3.7-flash"},
                clear=False,
            ):
                result = visual_qa_module.run_final_cut_visual_qa(
                    output_dir=output,
                    plan=plan,
                    script=script,
                    rights=rights,
                    fmt="film",
                    router=router,
                    visual_source=visual_source,
                )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["audited_selected_clip_count"], 3)
        self.assertEqual(result["visual_audit_count"], 4)
        self.assertEqual(router.calls, 1)
        self.assertEqual(visual_source.acquire_calls, 1)
        self.assertEqual(visual_source.requested_destination_names, ["visual-03.mp4"])
        self.assertEqual(visual_source.commit_calls, 1)
        self.assertEqual(visual_source.committed_destinations, ["visual-03.mp4"])
        # Only the third clip's own row was replaced; primary and the first
        # auxiliary were never touched by the second auxiliary's recovery.
        self.assertEqual(rights[0]["asset_id"], "primary-1")
        self.assertEqual(rights[1]["asset_id"], "aux-1")
        self.assertEqual(rights[2]["asset_id"], "recovered-1")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class VisualRecoveryCheckpointTests(unittest.TestCase):
    def test_successful_semantic_replacement_refreshes_visuals_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief = _brief()
            brief_path = root / "approved-brief.json"
            brief_path.write_text(
                json.dumps(brief, ensure_ascii=False),
                encoding="utf-8",
            )
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=_FakeVoice(),
                visual_source=_FakeVisuals(),
                visual_qa=_mutating_visual_qa,
                cinematic_layer=_blocking_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            with self.assertRaisesRegex(RuntimeError, "synthetic new layer block"):
                pipeline.run(
                    brief_path=brief_path,
                    approved_sha256=compute_brief_sha256(brief),
                    output_dir=output,
                    engine_sha="a" * 40,
                    runner_sha="b" * 40,
                    max_visuals=2,
                )

            checkpoint = json.loads(
                (output / "resume-checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["completed_stage"], "visuals")
            manifest = json.loads(
                (output / "rights-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["assets"][0]["asset_id"], "recovered-asset")
            relative = "visuals/fixture-1.mp4"
            current_hash = __import__("hashlib").sha256(
                (output / relative).read_bytes()
            ).hexdigest()
            self.assertEqual(checkpoint["artifacts"][relative], current_hash)


class OneBoundedToneRepairRun199Tests(unittest.TestCase):
    RUN199_TONE_BLOCK = {
        "status": "block",
        "validation": "valid",
        "preachiness_flags": [
            "s3: The narration shifts into a prescriptive tone with 'اشترك في القناة إذا كنت تريد أدوات واقعية لإدارة الوقت دون ضغوط' embedded mid-section, which feels like a push for subscription rather than reflective analysis.",
            "s5: The closing CTA is repeated verbatim from s3, and the phrasing 'خذ ما ينفعك من الفكرة' comes across as directive rather than reflective.",
        ],
        "naturalness_flags": [
            "s1: The phrase 'بسم الله' appears abruptly at the start of a secular productivity topic without contextual framing, making the tone feel inconsistent and potentially jarring to viewers expecting a neutral tone.",
            "s2: The term 'التخطيط fallacy' mixes Arabic and English unnecessarily, disrupting the natural flow of Modern Standard Arabic.",
            "s3: The phrase 'البحث عن الراحة اللحظية' is a direct transliteration of a Western psychological term without sufficient Arabic contextualization, making it feel foreign and less natural.",
            "s5: The phrase 'حفظكم الله' at the end of a secular productivity video is culturally appropriate but feels disconnected from the rest of the narrative tone, creating a tonal mismatch.",
        ],
        "narrative_format_flags": [
            "viewer_retention_continuity: s1 hook establishes a relatable daily scenario but s2 immediately pivots into abstract academic language ('التخطيط fallacy', 'تقدير الزمن') without building on the personal example introduced, creating a disconnect between the emotional hook and the analytical body.",
            "viewer_retention_continuity: s3 introduces procrastination as a new concept without clearly linking it back to the planning fallacy discussed in s2, making the progression feel segmented rather than cumulative.",
            "viewer_retention_continuity: s4 presents 'الخطط إذا-فإن' as a solution but does not explicitly connect it to the two prior problems (planning fallacy and procrastination), weakening the causal chain.",
            "editorial_promise_continuity: The hook and title promise an exploration of 'لماذا تفشل خططك' but s5 shifts into a direct call-to-action ('اشترك في القناة') and a generic encouragement to try one step, which feels more like a standard YouTube outro than an earned payoff to the central question.",
            "editorial_promise_continuity: The closing_payoff states 'الخطط إذا-فإن تربط الفعل' but the video only briefly mentions 'بإشارة محددة في اليوم' without citing specific studies or providing concrete evidence, making the payoff feel under-supported relative to the promise.",
        ],
        "unverified_religious_quote_flags": [],
    }

    OPENER = (
        "بسم الله، وسط ضجيج الحياة اليومية، نحتاج أحياناً إلى نداء يوقظ القلب والعقل. "
        "هذا هو نداء اليقظة."
    )
    CLOSER = (
        "وفي ختام هذا الفيديو، خذ ما ينفعك من الفكرة، وحوّل الوعي إلى خطوة عملية. "
        "حفظكم الله، وإلى نداءٍ جديد."
    )
    CTA = "اشترك في القناة إذا كنت تريد أدوات واقعية لإدارة الوقت دون ضغوط."
    HOOK = "في صباح يوم عادي، تضع خطة واضحة ليومك ثم تكتشف أن الوقت سبقها."

    @classmethod
    def _run199_script(cls) -> dict:
        return {
            "title": "لماذا تفشل خططك حتى عندما تكون مصممة بعناية؟",
            "sections": [
                {
                    "id": "s1",
                    "narration": (
                        cls.HOOK + " " + cls.OPENER
                        + " نبدأ من هذه الفجوة اليومية بين ما نتوقعه وما يحدث فعلًا."
                    ),
                },
                {
                    "id": "s2",
                    "narration": (
                        "التخطيط fallacy يجعل تقدير الزمن أكثر تفاؤلًا من الواقع، "
                        "فتبدو المهمة أقصر وأسهل مما ستكون عليه أثناء التنفيذ."
                    ),
                },
                {
                    "id": "s3",
                    "narration": (
                        "ثم يظهر التأجيل عندما تصبح المهمة ثقيلة، فنبحث عن الراحة اللحظية. "
                        + cls.CTA
                    ),
                },
                {
                    "id": "s4",
                    "narration": (
                        "الخطط إذا-فإن تربط الفعل بإشارة محددة في اليوم وتقلل مساحة القرار المتردد."
                    ),
                },
                {
                    "id": "s5",
                    "narration": (
                        "اختر خطوة صغيرة وراقب أثرها بهدوء قبل أن تبني عليها الخطوة التالية. "
                        + cls.CLOSER
                    ),
                },
            ],
        }

    @classmethod
    def _repaired_script(cls) -> dict:
        repaired = cls._run199_script()
        repaired["sections"][1]["narration"] = (
            "ينشأ أول خلل حين نقدّر زمن المهمة بتفاؤل زائد، فتبدو أقصر مما تكشفه "
            "المقاطعات والتفاصيل عند التنفيذ، وهذا يفسر الفجوة التي رأيناها في البداية."
        )
        repaired["sections"][2]["narration"] = (
            "ومع تراكم هذه الفجوة يصبح التأجيل أكثر احتمالًا، خصوصًا عندما تكون المهمة "
            "مزعجة فنبحث عن راحة سريعة بدل الاستمرار. " + cls.CTA
        )
        repaired["sections"][3]["narration"] = (
            "لهذا تأتي خطط إذا-فإن كحل مباشر للمشكلتين: فهي تضيف هامشًا واقعيًا "
            "وتربط البداية بإشارة محددة تقلل مساحة التأجيل."
        )
        repaired["sections"][4]["narration"] = (
            "جرّب شرطًا واحدًا اليوم، ثم راقب هل جعل البداية أوضح من خطتك السابقة. "
            + cls.CLOSER
        )
        return repaired

    @classmethod
    def _write_locked_runtime_files(cls, root: Path) -> None:
        (root / "narrative-identity.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "opener": cls.OPENER,
                    "closer": cls.CLOSER,
                    "transitions": ["أولاً", "ثم", "أخيرًا"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (root / "cta-plan.json").write_text(
            json.dumps(
                {
                    "contract_version": "contextual-cta-v1",
                    "mode": "subscribe",
                    "anchor_section_id": "s3",
                    "spoken_text": cls.CTA,
                    "visual_only": False,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    class _Router:
        def __init__(self, candidate: dict) -> None:
            self.candidate = candidate
            self.calls = 0
            self.prompts: list[str] = []

        def route(self, *, stage, prompt, max_tokens, validator):
            self.calls += 1
            self.prompts.append(prompt)
            if stage != "script_patch":
                raise AssertionError(stage)
            if max_tokens != 2200:
                raise AssertionError(max_tokens)
            if "patches" in self.candidate:
                return validator(self.candidate)

            context_marker = "PRODUCTION_CONTEXT:\n"
            revision_marker = "\n\nREVISION_NOTE:\n"
            raw_context = prompt.split(context_marker, 1)[1].split(revision_marker, 1)[0]
            context = json.loads(raw_context)
            current_script = context["current_script"]
            cta_plan = context["cta_plan"]
            revision_note = prompt.split(revision_marker, 1)[1].split(
                "\n\n[RESEARCH_BOUNDARIES]", 1
            )[0].strip()
            target_ids = set(
                _repair_target_section_ids(current_script, revision_note, cta_plan)
            )
            candidate_by_id = {
                str(item.get("id") or ""): item
                for item in (self.candidate.get("sections") or [])
                if isinstance(item, dict)
            }
            patches = []
            for item in current_script.get("sections") or []:
                section_id = str(item.get("id") or "")
                if section_id not in target_ids or section_id not in candidate_by_id:
                    continue
                original = str(item.get("narration") or "")
                replacement = str(candidate_by_id[section_id].get("narration") or "")
                if original == replacement:
                    continue
                patches.append(
                    {
                        "section_id": section_id,
                        "find": original,
                        "replace": replacement,
                    }
                )
            return validator({"patches": patches})

    def _plan_for_run199(self) -> dict:
        plan = _plan()
        plan["title"] = "لماذا تفشل خططك حتى عندما تكون مصممة بعناية؟"
        plan["cta"] = self.CTA
        return plan

    def test_run199_tone_block_gets_exactly_one_repair_then_full_reaudit_passes(self) -> None:
        audit_calls = {"n": 0}
        router = self._Router(self._repaired_script())

        def text_audit(**_kwargs):
            audit_calls["n"] += 1
            if audit_calls["n"] == 1:
                raise CleanV2ToneContentBlock(self.RUN199_TONE_BLOCK)
            return {
                "schema_version": 1,
                "status": "pass",
                "factuality_status": "pass",
                "tone_naturalness_status": "pass",
            }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_locked_runtime_files(root)
            script = self._run199_script()
            result = _run_text_audit_with_one_bounded_tone_repair(
                text_audit=text_audit,
                router=router,
                output_dir=root,
                brief=_brief(),
                plan=self._plan_for_run199(),
                script=script,
            )

            self.assertEqual(router.calls, 1)
            self.assertEqual(audit_calls["n"], 2)
            self.assertTrue(result["tone_repair_attempted"])
            self.assertEqual(result["tone_repair_attempts"], 1)
            self.assertEqual(result["tone_repair_status"], "repaired")
            self.assertEqual(result["post_repair_structural_ai_status"], "pass")
            self.assertEqual(script, validate_script(self._repaired_script(), self._plan_for_run199()))

            prompt = router.prompts[0]
            expected_flags = (
                self.RUN199_TONE_BLOCK["preachiness_flags"]
                + self.RUN199_TONE_BLOCK["naturalness_flags"]
                + self.RUN199_TONE_BLOCK["narrative_format_flags"]
            )
            for flag in expected_flags:
                self.assertIn("- [tone] " + flag, prompt)
            self.assertNotIn("[tone] cultural", prompt)
            self.assertIn("REVISION_NOTE:", prompt)
            self.assertIn("Preserve the runtime narrative-identity opener and closer exactly once each.", prompt)
            self.assertIn("Preserve the authored CTA spoken_text exactly once and in the same anchor section.", prompt)
            self.assertIn("Preserve all approved factual claims and their research boundaries.", prompt)

            repair = json.loads((root / "tone-repair.json").read_text(encoding="utf-8"))
            self.assertEqual(repair["status"], "repaired")
            self.assertEqual(repair["attempts"], 1)
            structural = json.loads((root / "structural-ai-flags.json").read_text(encoding="utf-8"))
            self.assertEqual(structural["flags"], [])

    def test_cohort7_inner_dialogue_block_gets_specific_contract_and_one_reaudit(self) -> None:
        tone_block = {
            "status": "block",
            "validation": "valid",
            "preachiness_flags": [
                "s3 narration uses direct imperative that feels preachy rather than reflective.",
            ],
            "naturalness_flags": [
                "Unnatural phrasing 'الصغيرة حتى' in s2 narration.",
            ],
            "narrative_format_flags": [
                "inner_dialogue not expressed naturally; narration reads as external advice rather than internal monologue.",
                "viewer_retention_continuity: Hook repeats same line in s1, no advancement after hook.",
                "viewer_retention_continuity: Payoff does not deliver inner_dialogue internal shift; repeats imperative without showing internal change.",
            ],
            "cultural_dignity_flags": [],
            "unverified_religious_quote_flags": [],
            "notes": [],
        }
        brief = {
            "approved_by_user": True,
            "approved_topic": "كيف تنهض عندما تفقد الدافع وتقول لنفسك لا أستطيع؟",
            "format": "short",
            "language": "ar",
            "audience": "Arabic-speaking adults",
            "editorial_intent": "نبرة هادئة وطبيعية.",
            "research_pack": [],
            "hard_constraints": ["No fabricated facts."],
        }
        plan = {
            "title": "عندما تفقد الدافع: كيف تبدأ من جديد؟",
            "promise": "تحول داخلي واحد يقود إلى خطوة صغيرة.",
            "cta": "",
            "sections": [
                {"id": "s1", "heading": "الصوت الداخلي", "purpose": "فتح التوتر", "visual_query_en": "quiet person thinking by window"},
                {"id": "s2", "heading": "الاحتكاك", "purpose": "إظهار ما يبقي التردد", "visual_query_en": "hands resting beside closed notebook"},
                {"id": "s3", "heading": "التحول", "purpose": "إنهاء التوتر بفعل واحد", "visual_query_en": "hand writing one word in notebook"},
            ],
        }
        original = {
            "title": plan["title"],
            "sections": [
                {"id": "s1", "narration": "لا تفقد الدافع، بل تفقد الخطوة الأولى فقط."},
                {"id": "s2", "narration": "الدافع لا ينتظرك. كل ما تحتاجه هو تلك الخطوة الأولى، الصغيرة حتى، لتكتشف أنه كان هناك من البداية."},
                {"id": "s3", "narration": "اكتب ما يأتي إلى ذهنك الآن، دون انتظار الإلهم. انظر كيف تتحول تلك الجملة إلى بداية."},
            ],
        }
        repaired_s2 = (
            "في داخلي قلت: لا أريد أن أبدأ؛ كنت أنتظر أن أشعر بالدافع أولًا. "
            "ثم لاحظت أن الانتظار نفسه هو ما يبقيني في مكاني."
        )
        repaired_s3 = (
            "عندما كتبت أول كلمة، لم أشعر أن الدافع عاد؛ فقط بدا البدء أقل ثقلًا. "
            "اكتب كلمة واحدة."
        )

        class ShortRouter:
            def __init__(self):
                self.calls = 0
                self.prompts = []

            def route(self, *, stage, prompt, max_tokens, validator):
                self.calls += 1
                self.prompts.append(prompt)
                self_outer.assertEqual(stage, "script_patch")
                self_outer.assertEqual(max_tokens, 1200)
                return validator(
                    {
                        "patches": [
                            {"section_id": "s2", "find": original["sections"][1]["narration"], "replace": repaired_s2},
                            {"section_id": "s3", "find": original["sections"][2]["narration"], "replace": repaired_s3},
                        ]
                    }
                )

        self_outer = self
        router = ShortRouter()
        audit_calls = {"n": 0}

        def text_audit(**_kwargs):
            audit_calls["n"] += 1
            if audit_calls["n"] == 1:
                raise CleanV2ToneContentBlock(tone_block)
            return {
                "schema_version": 1,
                "status": "pass",
                "factuality_status": "pass",
                "tone_naturalness_status": "pass",
            }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "narrative-identity.json").write_text(
                json.dumps({"opener": "", "closer": "", "transitions": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            (root / "cta-plan.json").write_text(
                json.dumps({"anchor_section_id": "", "spoken_text": "", "visual_only": True}, ensure_ascii=False),
                encoding="utf-8",
            )
            script = json.loads(json.dumps(original, ensure_ascii=False))
            result = _run_text_audit_with_one_bounded_tone_repair(
                text_audit=text_audit,
                router=router,
                output_dir=root,
                brief=brief,
                plan=plan,
                script=script,
            )

            self.assertEqual(router.calls, 1)
            self.assertEqual(audit_calls["n"], 2)
            self.assertEqual(result["tone_repair_attempts"], 1)
            self.assertEqual(result["post_repair_structural_ai_status"], "pass")
            self.assertNotEqual(script, original)
            self.assertEqual(script["sections"][0]["narration"], original["sections"][0]["narration"])
            self.assertEqual(script["sections"][1]["narration"], repaired_s2)
            self.assertEqual(script["sections"][2]["narration"], repaired_s3)

            prompt = router.prompts[0]
            for field in ("preachiness_flags", "naturalness_flags", "narrative_format_flags"):
                for flag in tone_block[field]:
                    self.assertIn("- [tone] " + flag, prompt)
            self.assertIn("direct advice disguised as inner_dialogue", prompt)
            self.assertIn("felt moment -> brief inner thought -> natural realization/turn -> one earned action", prompt)
            self.assertIn('BAD: "قلت لنفسي: السبب الحقيقي ليس الإرهاق، بل أنك لم تحدد ما تريد."', prompt)
            self.assertIn('GOOD: "مرّ اليوم ولم أبدأ. القائمة بدت أكبر مني. ربما أحتاج بداية أصغر."', prompt)
            self.assertIn('repeated "قلت لنفسي"', prompt)
            self.assertIn('"ليس X بل Y"', prompt)
            self.assertIn('Do not address the viewer with "افعل" / "ابدأ" / "عليك" except in the final line only', prompt)
            self.assertIn("not preached by an external narrator", prompt)

            repair = json.loads((root / "tone-repair.json").read_text(encoding="utf-8"))
            self.assertEqual(repair["attempts"], 1)
            self.assertEqual(repair["status"], "repaired")
            persisted = json.loads((root / "script-post-tone-repair.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted, script)

    def test_run256_tone_repair_targets_occurrences_and_hard_research_boundaries(self) -> None:
        original = self._run199_script()
        original["sections"][1]["narration"] = (
            "المشكلة ليست ضعف الإرادة بل سوء تقدير الوقت. "
            "والباقي جملة سليمة يجب أن تبقى كما هي."
        )
        original["sections"][2]["narration"] = (
            "ليس التأجيل كسلًا بل محاولة للهروب من مهمة منفرة. " + self.CTA
        )
        tone_block = {
            "status": "block",
            "preachiness_flags": [],
            "naturalness_flags": [],
            "narrative_format_flags": [
                "viewer_retention_continuity: The CTA in s3 is abrupt inside the locked anchor section.",
                "viewer_retention_continuity: The closing_payoff in s5 is too generic.",
            ],
            "unverified_religious_quote_flags": [],
        }
        plan = self._plan_for_run199()
        brief = _brief()
        brief["research_pack"] = [
            {
                "source_title": "Planning fallacy source",
                "claim_scope": (
                    "Use only to support the general tendency to underestimate task duration; "
                    "do not invent a concrete experiment or claim the brain is designed for it."
                ),
            },
            {
                "source_title": "Implementation intentions source",
                "claim_scope": (
                    "Use only to support association with better follow-through; "
                    "do not claim guaranteed success."
                ),
            },
        ]
        router = self._Router(self._repaired_script())
        audit_calls = {"n": 0}

        def text_audit(**_kwargs):
            audit_calls["n"] += 1
            if audit_calls["n"] == 1:
                raise CleanV2ToneContentBlock(tone_block)
            return {
                "schema_version": 1,
                "status": "pass",
                "factuality_status": "pass",
                "tone_naturalness_status": "pass",
            }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_locked_runtime_files(root)
            (root / "structural-ai-flags.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source": "legacy-editorial-room-structural-ai-flags",
                        "mode": "advisory",
                        "short_form": False,
                        "flags": ["repeated_not_x_but_y"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            script = original
            _run_text_audit_with_one_bounded_tone_repair(
                text_audit=text_audit,
                router=router,
                output_dir=root,
                brief=brief,
                plan=plan,
                script=script,
            )

        self.assertEqual(router.calls, 1)
        prompt = router.prompts[0]
        self.assertIn("[RESEARCH_BOUNDARIES]", prompt)
        self.assertIn("Planning fallacy source", prompt)
        self.assertIn("do not invent a concrete experiment", prompt)
        self.assertIn("[TARGETED_STRUCTURAL_REPAIR_CONTRACT]", prompt)
        self.assertIn("OFFENDING_OCCURRENCES=", prompt)
        self.assertIn("المشكلة ليست ضعف الإرادة بل سوء تقدير الوقت", prompt)
        self.assertIn("ليس التأجيل كسلًا بل محاولة للهروب", prompt)
        self.assertIn("Preserve every unaffected sentence exactly", prompt)
        self.assertIn("Tone repair is NOT permission to explain the science again", prompt)
        self.assertIn("participant group", prompt)
        self.assertIn('"the brain is designed to..."', prompt)

    def test_run254_keeps_exact_cta_in_natural_position_inside_locked_anchor(self) -> None:
        candidate = self._repaired_script()
        candidate["sections"][2]["narration"] = (
            "ومع تراكم الفجوة يصبح التأجيل أكثر احتمالًا. "
            + self.CTA
            + " ثم نعود مباشرة إلى الفكرة: المطلوب هو تقليل مساحة القرار عند لحظة البدء."
        )

        repaired = _validate_tone_repair_script(
            candidate,
            plan=self._plan_for_run199(),
            original_script=self._run199_script(),
            identity={
                "opener": self.OPENER,
                "closer": self.CLOSER,
                "transitions": ["أولاً", "ثم", "أخيرًا"],
            },
            cta_plan={
                "anchor_section_id": "s3",
                "spoken_text": self.CTA,
            },
        )

        anchor = repaired["sections"][2]["narration"]
        self.assertEqual(anchor.count(self.CTA), 1)
        self.assertIn(self.CTA + " ثم نعود مباشرة إلى الفكرة", anchor)
        self.assertFalse(anchor.rstrip().endswith(self.CTA))
        joined = "\n".join(item["narration"] for item in repaired["sections"])
        self.assertEqual(joined.count(self.CTA), 1)

    def test_run222_combines_structural_flag_into_single_repair_and_persists_candidate(self) -> None:
        from clean_v2.structural_ai import structural_ai_flags

        original = self._run199_script()
        s2_before = (
            "ليس لأن المهمة سهلة، بل لأن تقديرنا للوقت متفائل أكثر من اللازم. "
            "ليس لأننا نتعمد التأخير، بل لأن التفاصيل تظهر أثناء التنفيذ."
        )
        s3_before = (
            "ليس لأن الإرادة غائبة، بل لأن الراحة اللحظية تصبح أكثر جاذبية عند الضغط."
        )
        original["sections"][1]["narration"] = s2_before
        original["sections"][2]["narration"] = s3_before + " " + self.CTA
        original["sections"][3]["narration"] = (
            "اربط البداية بإشارة واضحة في يومك حتى تصبح الخطوة محددة بدل أن تبقى نية عامة."
        )

        candidate = {
            "patches": [
                {
                    "section_id": "s2",
                    "find": s2_before,
                    "replace": (
                        "قد يكون تقديرنا للوقت متفائلًا أكثر من اللازم، "
                        "ثم تظهر أثناء التنفيذ تفاصيل لم تدخل في الحساب الأول."
                    ),
                },
                {
                    "section_id": "s3",
                    "find": s3_before,
                    "replace": (
                        "عند الضغط قد تصبح الراحة اللحظية أكثر جاذبية، "
                        "فيظهر التأجيل من دون تحويله إلى حكم أخلاقي."
                    ),
                },
            ]
        }
        audit_calls = {"n": 0}
        router = self._Router(candidate)

        def text_audit(**_kwargs):
            audit_calls["n"] += 1
            if audit_calls["n"] == 1:
                raise CleanV2ToneContentBlock(self.RUN199_TONE_BLOCK)
            return {
                "schema_version": 1,
                "status": "pass",
                "factuality_status": "pass",
                "tone_naturalness_status": "pass",
            }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_locked_runtime_files(root)
            transcript = "\n\n".join(
                item["narration"] for item in original["sections"]
            )
            initial_flags = list(structural_ai_flags(transcript, short_form=False))
            self.assertIn("repeated_not_x_but_y", initial_flags)
            (root / "structural-ai-flags.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source": "legacy-editorial-room-structural-ai-flags",
                        "mode": "advisory",
                        "short_form": False,
                        "flags": initial_flags,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            script = original
            result = _run_text_audit_with_one_bounded_tone_repair(
                text_audit=text_audit,
                router=router,
                output_dir=root,
                brief=_brief(),
                plan=self._plan_for_run199(),
                script=script,
            )

            self.assertEqual(router.calls, 1)
            self.assertEqual(audit_calls["n"], 2)
            self.assertEqual(result["tone_repair_attempts"], 1)
            self.assertEqual(result["post_repair_structural_ai_status"], "pass")

            prompt = router.prompts[0]
            self.assertIn("- [tone] ", prompt)
            self.assertIn(
                "- [structural] repeated_not_x_but_y: eliminate repeated Arabic contrast",
                prompt,
            )
            self.assertIn('"ليس X بل Y"', prompt)
            self.assertIn("ALLOWED_PATCH_SECTION_IDS:", prompt)

            persisted = json.loads(
                (root / "script-post-tone-repair.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted, script)
            joined = "\n".join(item["narration"] for item in script["sections"])
            self.assertEqual(joined.count(self.OPENER), 1)
            self.assertEqual(joined.count(self.CLOSER), 1)
            self.assertEqual(joined.count(self.CTA), 1)
            self.assertEqual(script["title"], original["title"])

            post_structural = json.loads(
                (root / "structural-ai-flags.json").read_text(encoding="utf-8")
            )
            self.assertEqual(post_structural["flags"], [])
            self.assertNotIn(
                "repeated_not_x_but_y",
                structural_ai_flags(joined, short_form=False),
            )

    def test_run245_factuality_block_gets_one_bounded_repair_then_full_reaudit_passes(self) -> None:
        from clean_v2.structural_ai import structural_ai_flags

        factuality_block = {
            "schema_version": 1,
            "source": "clean-v2-legacy-factuality-audit",
            "status": "block",
            "unsupported_claims": [
                {"section_id": "s4", "issue": "'هناك ما يضمن أن هذا سيجعلك أكثر احتمالًا للنجاح' overstates evidence that supports improved follow-through/probability, not a guarantee."},
                {"section_id": "s5", "issue": "'هناك ما يضمن أن هذا سيجعلك أكثر احتمالًا للنجاح' overstates evidence that supports improved follow-through/probability, not a guarantee."},
            ],
            "diagnostics": {"raw_result": {
                "unsupported_claims": [
                    {"section_id": "s4", "issue": "'هناك ما يضمن أن هذا سيجعلك أكثر احتمالًا للنجاح' overstates evidence that supports improved follow-through/probability, not a guarantee."},
                    {"section_id": "s5", "issue": "'هناك ما يضمن أن هذا سيجعلك أكثر احتمالًا للنجاح' overstates evidence that supports improved follow-through/probability, not a guarantee."},
                ], "professional_advice_flags": [], "expert_persona_flags": []
            }, "validation": "valid"},
            "professional_advice_flags": [],
            "expert_persona_flags": [],
            "notes": [],
            }
        original = self._run199_script()
        repeated_claim = "هناك ما يضمن أن هذا سيجعلك أكثر احتمالًا للنجاح."
        s2_before = (
            "ليست المشكلة ضعف الإرادة بل أن تقدير الوقت يكون متفائلًا أحيانًا. "
            "وليس التعثر دليلًا على الكسل بل نتيجة لفجوة بين التوقع والتنفيذ. "
            "وليس الحل ضغطًا أكبر بل ربط البداية بإشارة أوضح."
        )
        original["sections"][1]["narration"] = s2_before
        original["sections"][3]["narration"] = (
            "اربط البداية بوقت أو موقف واضح. " + repeated_claim
        )
        original["sections"][4]["narration"] = repeated_claim + " " + self.CLOSER

        candidate = {
            "patches": [
                {
                    "section_id": "s2",
                    "find": s2_before,
                    "replace": (
                        "قد يكون تقدير الوقت متفائلًا أحيانًا، وتظهر أثناء التنفيذ فجوة "
                        "بين التوقع وما تسمح به تفاصيل اليوم. ويمكن جعل البداية أوضح "
                        "بربطها بإشارة محددة."
                    ),
                },
                {
                    "section_id": "s4",
                    "find": repeated_claim,
                    "replace": (
                        "تشير الأدلة المعتمدة إلى أن ربط الفعل بوقت أو موقف محدد "
                        "قد يزيد احتمال المتابعة والتنفيذ."
                    ),
                },
                {
                    "section_id": "s5",
                    "find": repeated_claim,
                    "replace": (
                        "راقب أثر الإشارة على المتابعة قبل أن توسع الخطة."
                    ),
                },
            ]
        }
        audit_calls = {"n": 0}
        router = self._Router(candidate)

        def text_audit(**_kwargs):
            audit_calls["n"] += 1
            if audit_calls["n"] == 1:
                raise CleanV2FactualityContentBlock(factuality_block)
            return {
                "schema_version": 1,
                "status": "pass",
                "factuality_status": "pass",
                "tone_naturalness_status": "pass",
            }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_locked_runtime_files(root)
            transcript = "\n\n".join(
                item["narration"] for item in original["sections"]
            )
            initial_flags = list(structural_ai_flags(transcript, short_form=False))
            self.assertIn("repeated_not_x_but_y", initial_flags)
            self.assertIn("duplicate_sentence", initial_flags)
            (root / "structural-ai-flags.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source": "legacy-editorial-room-structural-ai-flags",
                        "mode": "advisory",
                        "short_form": False,
                        "flags": initial_flags,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            script = original
            result = _run_text_audit_with_one_bounded_tone_repair(
                text_audit=text_audit,
                router=router,
                output_dir=root,
                brief=_brief(),
                plan=self._plan_for_run199(),
                script=script,
            )

            self.assertEqual(router.calls, 1)
            self.assertEqual(audit_calls["n"], 2)
            self.assertTrue(result["factuality_repair_attempted"])
            self.assertEqual(result["factuality_repair_attempts"], 1)
            self.assertEqual(result["factuality_repair_status"], "repaired")
            self.assertEqual(result["post_repair_structural_ai_status"], "pass")

            prompt = router.prompts[0]
            self.assertIn("- [factuality:s4]", prompt)
            self.assertIn("- [factuality:s5]", prompt)
            self.assertIn("- [structural] repeated_not_x_but_y:", prompt)
            self.assertIn("- [structural] duplicate_sentence", prompt)
            self.assertIn("weaken, qualify, or remove only the offending wording", prompt)
            self.assertIn("Do not invent a new study", prompt)
            self.assertIn('"s2"', prompt)
            self.assertIn('"s4"', prompt)
            self.assertIn('"s5"', prompt)

            persisted = json.loads(
                (root / "script-post-factuality-repair.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(persisted, script)
            joined = "\n".join(item["narration"] for item in script["sections"])
            self.assertNotIn("يضمن", joined)
            self.assertIn("قد يزيد احتمال المتابعة والتنفيذ", joined)
            self.assertEqual(joined.count(self.OPENER), 1)
            self.assertEqual(joined.count(self.CLOSER), 1)
            self.assertEqual(joined.count(self.CTA), 1)
            self.assertEqual(script["title"], original["title"])

            repair = json.loads(
                (root / "factuality-repair.json").read_text(encoding="utf-8")
            )
            self.assertEqual(repair["status"], "repaired")
            self.assertEqual(repair["attempts"], 1)
            post_structural = json.loads(
                (root / "structural-ai-flags.json").read_text(encoding="utf-8")
            )
            self.assertEqual(post_structural["flags"], [])

    def test_run249_composite_audit_collects_tone_before_factuality_repair(self) -> None:
        factuality_block = {
            "status": "block",
            "unsupported_claims": [{"section_id": "s3", "issue": "claim exceeds approved evidence"}],
            "professional_advice_flags": [],
            "expert_persona_flags": [],
            "diagnostics": {"raw_result": {"unsupported_claims": [{"section_id": "s3", "issue": "claim exceeds approved evidence"}], "professional_advice_flags": [], "expert_persona_flags": []}},
        }
        tone_block = {
            "status": "block",
            "preachiness_flags": ["s1 opener is overly promotional"],
            "naturalness_flags": ["s3 CTA is abrupt and promotional"],
            "narrative_format_flags": [
                "viewer_retention_continuity: s1 repeats the hook without advancing value"
            ],
            "unverified_religious_quote_flags": [],
        }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch(
                "clean_v2.pipeline._run_legacy_factuality_audit",
                side_effect=CleanV2FactualityContentBlock(factuality_block),
            ) as factuality_call, mock.patch(
                "clean_v2.pipeline._run_legacy_tone_naturalness_audit",
                side_effect=CleanV2ToneContentBlock(tone_block),
            ) as tone_call:
                with self.assertRaises(CleanV2FactualityContentBlock) as raised:
                    _run_text_audits(
                        output_dir=root,
                        brief=_brief(),
                        plan=self._plan_for_run199(),
                        script=self._run199_script(),
                    )

            self.assertEqual(factuality_call.call_count, 1)
            self.assertEqual(tone_call.call_count, 1)
            self.assertEqual(raised.exception.report, factuality_block)
            self.assertEqual(raised.exception.tone_report, tone_block)

    def test_run249_factuality_tone_and_structural_share_one_repair(self) -> None:
        factuality_block = {
            "status": "block",
            "unsupported_claims": [{"section_id": "s3", "issue": "specificity was stated more strongly than the approved association."}],
            "diagnostics": {"raw_result": {"unsupported_claims": [{"section_id": "s3", "issue": "specificity was stated more strongly than the approved association."}], "professional_advice_flags": [], "expert_persona_flags": []}},
            "professional_advice_flags": [],
            "expert_persona_flags": [],
        }
        tone_block = {
            "status": "block",
            "preachiness_flags": [
                "Opening narration reads as overly promotional rather than reflective."
            ],
            "naturalness_flags": [
                "The CTA in s3 feels abrupt and promotional."
            ],
            "narrative_format_flags": [
                "viewer_retention_continuity: s1 repeats the hook without advancing causal understanding."
            ],
            "unverified_religious_quote_flags": [],
        }
        candidate = {
            "patches": [
                {
                    "section_id": "s1",
                    "find": "نبدأ من هذه الفجوة اليومية بين ما نتوقعه وما يحدث فعلًا.",
                    "replace": (
                        "هذه الفجوة اليومية بين التوقع والتنفيذ هي نقطة البداية للسؤال."
                    ),
                },
                {
                    "section_id": "s3",
                    "find": (
                        "ثم يظهر التأجيل عندما تصبح المهمة ثقيلة، فنبحث عن الراحة اللحظية."
                    ),
                    "replace": (
                        "وعندما تصبح المهمة ثقيلة قد يظهر التأجيل بحثًا عن راحة سريعة."
                    ),
                },
            ]
        }
        router = self._Router(candidate)
        audit_calls = {"n": 0}

        def text_audit(**_kwargs):
            audit_calls["n"] += 1
            if audit_calls["n"] == 1:
                raise CleanV2FactualityContentBlock(
                    factuality_block,
                    tone_report=tone_block,
                )
            return {
                "schema_version": 1,
                "status": "pass",
                "factuality_status": "pass",
                "tone_naturalness_status": "pass",
            }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_locked_runtime_files(root)
            (root / "structural-ai-flags.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source": "legacy-editorial-room-structural-ai-flags",
                        "mode": "advisory",
                        "short_form": False,
                        "flags": ["duplicate_sentence"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            script = self._run199_script()
            result = _run_text_audit_with_one_bounded_tone_repair(
                text_audit=text_audit,
                router=router,
                output_dir=root,
                brief=_brief(),
                plan=self._plan_for_run199(),
                script=script,
            )

            self.assertEqual(router.calls, 1)
            self.assertEqual(audit_calls["n"], 2)
            self.assertEqual(result["factuality_repair_attempts"], 1)
            prompt = router.prompts[0]
            self.assertIn("- [factuality:s3]", prompt)
            self.assertIn("- [tone] Opening narration reads as overly promotional", prompt)
            self.assertIn("- [tone] The CTA in s3 feels abrupt and promotional", prompt)
            self.assertIn("- [tone] viewer_retention_continuity:", prompt)
            self.assertIn("- [structural] duplicate_sentence", prompt)
            self.assertIn(
                "Fix EVERY concrete factuality, tone/naturalness, and structural problem",
                prompt,
            )
            self.assertIn("ALLOWED_PATCH_SECTION_IDS:", prompt)
            self.assertTrue(
                (root / "tone-naturalness-audit-pre-repair.json").is_file()
            )
            saved_tone = json.loads(
                (root / "tone-naturalness-audit-pre-repair.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(saved_tone, tone_block)

    def test_run245_factuality_repair_prompt_is_mistral_script_schema_compatible(self) -> None:
        identity = {
            "opener": self.OPENER,
            "closer": self.CLOSER,
            "transitions": ["أولاً", "ثم", "أخيرًا"],
        }
        cta_plan = {
            "mode": "subscribe",
            "anchor_section_id": "s3",
            "spoken_text": self.CTA,
            "visual_only": False,
        }
        prompt = _factuality_repair_prompt(
            brief=_brief(),
            plan=self._plan_for_run199(),
            script=self._run199_script(),
            identity=identity,
            cta_plan=cta_plan,
            revision_note="- [factuality] s4: guarantee exceeds evidence",
        )
        patch_payload = {
            "patches": [
                {
                    "section_id": "s4",
                    "find": "الخطط إذا-فإن",
                    "replace": "خطط إذا-فإن",
                }
            ]
        }

        with mock.patch.object(
            providers_module.mistral_executor,
            "mistral_executor_json",
            return_value=patch_payload,
        ) as executor:
            result = providers_module._mistral_call(prompt, 2200, "script_patch")

        self.assertEqual(result, patch_payload)
        kwargs = executor.call_args.kwargs
        self.assertEqual(kwargs["task_kind"], "script_patch")
        schema_name, schema = kwargs["response_schema"]
        self.assertEqual(schema_name, "script_patch")
        patches = schema["properties"]["patches"]
        self.assertEqual(patches["maxItems"], 6)
        patch_item = patches["items"]["properties"]
        self.assertEqual(patch_item["find"]["maxLength"], 400)
        self.assertEqual(patch_item["replace"]["maxLength"], 550)

    def test_run245_factuality_repair_is_strictly_one_shot(self) -> None:
        factuality_block = {
            "status": "block",
            "unsupported_claims": [{"section_id": "s4", "issue": "guarantee exceeds evidence"}],
            "diagnostics": {"raw_result": {"unsupported_claims": [{"section_id": "s4", "issue": "guarantee exceeds evidence"}], "professional_advice_flags": [], "expert_persona_flags": []}},
            "professional_advice_flags": [],
            "expert_persona_flags": [],
        }
        audit_calls = {"n": 0}
        original = self._run199_script()
        old_s4 = original["sections"][3]["narration"]
        router = self._Router({
            "patches": [{
                "section_id": "s4",
                "find": old_s4,
                "replace": "اربط البداية بإشارة محددة في اليوم، ثم راقب النتيجة دون ضمان مسبق.",
            }]
        })

        def text_audit(**_kwargs):
            audit_calls["n"] += 1
            raise CleanV2FactualityContentBlock(factuality_block)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_locked_runtime_files(root)
            with self.assertRaisesRegex(
                RuntimeError,
                "Independent factuality/AI-expert gate blocked real production",
            ):
                _run_text_audit_with_one_bounded_tone_repair(
                    text_audit=text_audit,
                    router=router,
                    output_dir=root,
                    brief=_brief(),
                    plan=self._plan_for_run199(),
                    script=self._run199_script(),
                )

            self.assertEqual(router.calls, 1)
            self.assertEqual(audit_calls["n"], 2)

    def test_run220_host_overlay_keeps_naturalness_fix_and_restores_all_locked_anchors(self) -> None:
        original = self._run199_script()
        original["sections"][0]["narration"] = (
            self.HOOK + " " + self.OPENER
            + " 你看 planner اليوم يكشف فجوة بين الخطة والتنفيذ."
        )
        original["sections"][1]["narration"] = (
            "التخطيط fallacy يجعل تقدير الزمن أكثر تفاؤلًا من الواقع."
        )
        original["sections"][2]["narration"] = (
            "implementation intentions تساعد على ربط النية بإشارة محددة. "
            + self.CTA
        )
        original["sections"][4]["narration"] = (
            "planner ليس كافيًا وحده. " + self.CLOSER
        )

        candidate = {
            "title": "عنوان غير مسموح بتغييره",
            "sections": [
                {
                    "id": "s1",
                    "narration": (
                        "هوك بديل غير مسموح. انظر إلى مخططك اليوم، فهو يكشف فجوة "
                        "بين الخطة والتنفيذ."
                    ),
                },
                {
                    "id": "s2",
                    "narration": "مغالطة التخطيط تجعل تقدير الزمن أكثر تفاؤلًا من الواقع.",
                },
                {
                    "id": "s3",
                    "narration": "تساعد نوايا التنفيذ على ربط النية بإشارة محددة.",
                },
                {
                    "id": "s4",
                    "narration": "اربط البداية بوقت ومكان واضحين، ثم راقب ما يحدث.",
                },
                {
                    "id": "s5",
                    "narration": "المخطط وحده لا يكفي؛ المهم أن تصمم إشارة عملية للبدء.",
                },
            ],
        }
        identity = {
            "opener": self.OPENER,
            "closer": self.CLOSER,
            "transitions": ["أولاً", "ثم", "أخيرًا"],
        }
        cta_plan = {
            "mode": "subscribe",
            "anchor_section_id": "s3",
            "spoken_text": self.CTA,
            "visual_only": False,
        }

        repaired = _validate_tone_repair_script(
            candidate,
            plan=self._plan_for_run199(),
            original_script=original,
            identity=identity,
            cta_plan=cta_plan,
        )

        joined = "\n".join(item["narration"] for item in repaired["sections"])
        self.assertEqual(repaired["title"], original["title"])
        self.assertEqual(
            repaired["sections"][0]["narration"].split(self.OPENER, 1)[0].strip(),
            self.HOOK,
        )
        self.assertEqual(joined.count(self.OPENER), 1)
        self.assertEqual(joined.count(self.CLOSER), 1)
        self.assertEqual(joined.count(self.CTA), 1)
        self.assertIn(self.CTA, repaired["sections"][2]["narration"])
        self.assertIn("مغالطة التخطيط", repaired["sections"][1]["narration"])
        self.assertIn("نوايا التنفيذ", repaired["sections"][2]["narration"])
        self.assertNotIn("你看", joined)
        self.assertNotIn("planning fallacy", joined)
        self.assertNotIn("implementation intentions", joined)

    # Production regression: Cold Runs #201/#203 reached Tone repair but the
    # Mistral Script adapter rejected the repair prompt before wire because its
    # canonical LOCKED_PLAN boundary was absent.
    def test_run199_tone_repair_prompt_is_mistral_script_schema_compatible(self) -> None:
        identity = {
            "opener": self.OPENER,
            "closer": self.CLOSER,
            "transitions": ["أولاً", "ثم", "أخيرًا"],
        }
        cta_plan = {
            "mode": "subscribe",
            "anchor_section_id": "s3",
            "spoken_text": self.CTA,
            "visual_only": False,
        }
        prompt = _tone_repair_prompt(
            brief=_brief(),
            plan=self._plan_for_run199(),
            script=self._run199_script(),
            identity=identity,
            cta_plan=cta_plan,
            revision_note="- [tone] synthetic Run #199 regression flag",
        )

        with mock.patch.object(
            providers_module.mistral_executor,
            "mistral_executor_json",
            return_value=self._repaired_script(),
        ) as executor:
            result = providers_module._mistral_call(prompt, 7500, "script")

        self.assertEqual(result, self._repaired_script())
        kwargs = executor.call_args.kwargs
        self.assertEqual(kwargs["task_kind"], "script")
        schema_name, schema = kwargs["response_schema"]
        self.assertEqual(schema_name, "script")
        section_items = schema["properties"]["sections"]["prefixItems"]
        self.assertEqual(
            [item["properties"]["id"]["const"] for item in section_items],
            ["s1", "s2", "s3", "s4", "s5"],
        )

    def test_run199_repair_is_strictly_one_shot_and_fails_closed_if_tone_still_blocks(self) -> None:
        audit_calls = {"n": 0}
        router = self._Router(self._repaired_script())

        def text_audit(**_kwargs):
            audit_calls["n"] += 1
            raise CleanV2ToneContentBlock(self.RUN199_TONE_BLOCK)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_locked_runtime_files(root)
            script = self._run199_script()
            with self.assertRaisesRegex(
                CleanV2ToneContentBlock,
                "Independent tone/naturalness gate blocked real production",
            ):
                _run_text_audit_with_one_bounded_tone_repair(
                    text_audit=text_audit,
                    router=router,
                    output_dir=root,
                    brief=_brief(),
                    plan=self._plan_for_run199(),
                    script=script,
                )

            self.assertEqual(router.calls, 1)
            self.assertEqual(audit_calls["n"], 2)
            repair = json.loads((root / "tone-repair.json").read_text(encoding="utf-8"))
            self.assertEqual(repair["status"], "failed_closed")
            self.assertEqual(repair["attempts"], 1)

    def test_factuality_block_never_enters_tone_repair(self) -> None:
        router = self._Router(self._repaired_script())

        def factuality_block(**_kwargs):
            raise RuntimeError("Independent factuality/AI-expert gate blocked real production")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_locked_runtime_files(root)
            with self.assertRaisesRegex(RuntimeError, "factuality/AI-expert"):
                _run_text_audit_with_one_bounded_tone_repair(
                    text_audit=factuality_block,
                    router=router,
                    output_dir=root,
                    brief=_brief(),
                    plan=self._plan_for_run199(),
                    script=self._run199_script(),
                )
            self.assertEqual(router.calls, 0)
            self.assertFalse((root / "tone-repair.json").exists())


class OneBoundedToneRepairRun17WholeScriptFlagTests(unittest.TestCase):
    """Run #17: the blocked tone audit's only flag described the whole draft
    ("narrative format inner_dialogue not expressed naturally; script is
    monologue") with no section number and no quoted excerpt to locate it.
    _repair_target_section_ids returned no targets, so the bounded repair
    raised RuntimeError before any repair provider was even called. A
    whole-draft complaint has no single section by definition, so the
    deterministic fallback must offer every section as the allowed patch
    scope instead of failing closed pre-attempt.
    """

    TONE_BLOCK = {
        "status": "block",
        "validation": "valid",
        "preachiness_flags": [],
        "naturalness_flags": [],
        "narrative_format_flags": [
            "viewer_retention_continuity: narrative format inner_dialogue not "
            "expressed naturally; script is monologue"
        ],
        "cultural_dignity_flags": [],
        "unverified_religious_quote_flags": [],
        "notes": [],
    }
    BRIEF = {
        "approved_by_user": True,
        "approved_topic": "كيف تنهض عندما تفقد الدافع تمامًا؟",
        "format": "short",
        "language": "ar",
        "audience": "Arabic-speaking adults",
        "editorial_intent": "نبرة هادئة وطبيعية.",
        "research_pack": [],
        "hard_constraints": ["No fabricated facts."],
    }
    PLAN = {
        "title": "كيف تنهض عندما تفقد الدافع؟",
        "promise": "تحول داخلي واحد يقود إلى خطوة صغيرة.",
        "cta": "",
        "sections": [
            {"id": "s1", "heading": "الصوت الداخلي", "purpose": "فتح التوتر", "visual_query_en": "quiet person thinking"},
            {"id": "s2", "heading": "الاحتكاك", "purpose": "إظهار ما يبقي التردد", "visual_query_en": "hands resting beside notebook"},
            {"id": "s3", "heading": "التحول", "purpose": "إنهاء التوتر بفعل واحد", "visual_query_en": "hand writing one word"},
        ],
    }
    ORIGINAL = {
        "title": PLAN["title"],
        "sections": [
            {"id": "s1", "narration": "أشعر أن الدافع يختفي كلما احتجته"},
            {"id": "s2", "narration": "أدرك أن السبب ليس نقص الدافع، بل توقع النتيجة"},
            {"id": "s3", "narration": "اكتب هدفًا صغيرًا على ملصق وضعه على الثلاجة"},
        ],
    }

    def test_repair_target_section_ids_falls_back_to_all_sections_for_whole_script_flag(
        self,
    ) -> None:
        cta_plan = {"anchor_section_id": "", "spoken_text": ""}
        revision_note = "- [tone] " + self.TONE_BLOCK["narrative_format_flags"][0]
        target_ids = _repair_target_section_ids(self.ORIGINAL, revision_note, cta_plan)
        self.assertEqual(target_ids, ("s1", "s2", "s3"))

    def test_repair_target_section_ids_still_empty_with_no_issue_text(self) -> None:
        cta_plan = {"anchor_section_id": "", "spoken_text": ""}
        self.assertEqual(_repair_target_section_ids(self.ORIGINAL, "", cta_plan), ())

    def test_run17_whole_script_monologue_flag_gets_a_real_repair_attempt(self) -> None:
        repaired_s3 = (
            "قلت لنفسي: لماذا أؤجل هذا؟ ثم كتبت هدفًا صغيرًا ووضعته أمامي كل صباح."
        )

        class ShortRouter:
            def __init__(self):
                self.calls = 0
                self.prompts = []

            def route(self, *, stage, prompt, max_tokens, validator):
                self.calls += 1
                self.prompts.append(prompt)
                self_outer.assertEqual(stage, "script_patch")
                self_outer.assertEqual(max_tokens, 1200)
                return validator(
                    {
                        "patches": [
                            {
                                "section_id": "s3",
                                "find": self_outer.ORIGINAL["sections"][2]["narration"],
                                "replace": repaired_s3,
                            }
                        ]
                    }
                )

        self_outer = self
        router = ShortRouter()
        audit_calls = {"n": 0}

        def text_audit(**_kwargs):
            audit_calls["n"] += 1
            if audit_calls["n"] == 1:
                raise CleanV2ToneContentBlock(self.TONE_BLOCK)
            return {
                "schema_version": 1,
                "status": "pass",
                "factuality_status": "pass",
                "tone_naturalness_status": "pass",
            }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "narrative-identity.json").write_text(
                json.dumps(
                    {"opener": "", "closer": "", "transitions": []}, ensure_ascii=False
                ),
                encoding="utf-8",
            )
            (root / "cta-plan.json").write_text(
                json.dumps(
                    {"anchor_section_id": "", "spoken_text": "", "visual_only": True},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (root / "structural-ai-flags.json").write_text(
                json.dumps({"flags": [], "mode": "advisory"}, ensure_ascii=False),
                encoding="utf-8",
            )
            script = json.loads(json.dumps(self.ORIGINAL, ensure_ascii=False))
            result = _run_text_audit_with_one_bounded_tone_repair(
                text_audit=text_audit,
                router=router,
                output_dir=root,
                brief=self.BRIEF,
                plan=self.PLAN,
                script=script,
            )

            self.assertEqual(router.calls, 1)
            self.assertEqual(audit_calls["n"], 2)
            self.assertEqual(result["tone_repair_attempts"], 1)
            self.assertEqual(result["post_repair_structural_ai_status"], "pass")
            self.assertEqual(script["sections"][2]["narration"], repaired_s3)
            self.assertEqual(
                script["sections"][0]["narration"], self.ORIGINAL["sections"][0]["narration"]
            )
            self.assertEqual(
                script["sections"][1]["narration"], self.ORIGINAL["sections"][1]["narration"]
            )


class HookAuditVerifiedWordFixTests(unittest.TestCase):
    """Run #18: s1 was a single sentence, so it was also the entire locked
    hook. A validated naturalness flag ("Unnatural Arabic phrase 'أستكين' in
    narration s1; likely a typo or non-standard verb.") quoted a real,
    verified word inside that sentence, but any patch touching s1 was
    rejected outright by the hard hook-equality check, so the run failed
    even though every provider that responded (Mistral) proposed exactly the
    minimal fix the audit asked for. A short, audit-quoted word/phrase fix
    inside the hook must now be accepted once; anything broader must still
    be rejected.
    """

    PLAN = {
        "title": "كيف تنهض عندما تفقد الدافع؟",
        "promise": "تحول داخلي واحد يقود إلى خطوة صغيرة.",
        "cta": "",
        "sections": [
            {"id": "s1", "heading": "الصوت الداخلي", "purpose": "فتح التوتر", "visual_query_en": "quiet person thinking"},
            {"id": "s2", "heading": "الاحتكاك", "purpose": "إظهار ما يبقي التردد", "visual_query_en": "hands resting beside notebook"},
            {"id": "s3", "heading": "التحول", "purpose": "إنهاء التوتر بفعل واحد", "visual_query_en": "hand writing one word"},
        ],
    }
    ORIGINAL_HOOK = "أشعر أنني أستكين في مكان واحد بينما أريد أن أتحرك."
    ORIGINAL = {
        "title": PLAN["title"],
        "sections": [
            {"id": "s1", "narration": ORIGINAL_HOOK},
            {
                "id": "s2",
                "narration": (
                    "أفكر أن لا أبدأ إلا عندما أستشعر الحماس، لكني أجد نفسي أنتظر شعورًا لن يأتي."
                ),
            },
            {"id": "s3", "narration": "ابدأ بكتابة جملة واحدة في دفتر."},
        ],
    }
    IDENTITY = {"opener": "", "closer": "", "transitions": []}
    CTA_PLAN = {"anchor_section_id": "", "spoken_text": ""}
    REVISION_NOTE = (
        "- [tone] Unnatural Arabic phrase 'أستكين' in narration s1; likely a typo or "
        "non-standard verb."
    )

    def _patch_value(self, find: str, replace: str) -> dict:
        return {"patches": [{"section_id": "s1", "find": find, "replace": replace}]}

    def test_short_audit_verified_word_fix_inside_hook_is_accepted(self) -> None:
        script = json.loads(json.dumps(self.ORIGINAL, ensure_ascii=False))
        repaired = _validate_and_apply_script_patches(
            self._patch_value("أستكين", "أتجمّد"),
            plan=self.PLAN,
            original_script=script,
            identity=self.IDENTITY,
            cta_plan=self.CTA_PLAN,
            revision_note=self.REVISION_NOTE,
        )
        self.assertEqual(
            repaired["sections"][0]["narration"],
            "أشعر أنني أتجمّد في مكان واحد بينما أريد أن أتحرك.",
        )

    def test_unquoted_word_inside_hook_is_still_rejected(self) -> None:
        script = json.loads(json.dumps(self.ORIGINAL, ensure_ascii=False))
        with self.assertRaisesRegex(ValueError, "script patch changed the locked hook"):
            _validate_and_apply_script_patches(
                self._patch_value("أشعر", "أحس"),
                plan=self.PLAN,
                original_script=script,
                identity=self.IDENTITY,
                cta_plan=self.CTA_PLAN,
                revision_note=self.REVISION_NOTE,
            )

    def test_full_hook_rewrite_disguised_as_a_fix_is_still_rejected(self) -> None:
        script = json.loads(json.dumps(self.ORIGINAL, ensure_ascii=False))
        with self.assertRaisesRegex(ValueError, "script patch changed the locked hook"):
            _validate_and_apply_script_patches(
                self._patch_value(self.ORIGINAL_HOOK, "جملة مختلفة كليًا تستبدل الخطاف."),
                plan=self.PLAN,
                original_script=script,
                identity=self.IDENTITY,
                cta_plan=self.CTA_PLAN,
                revision_note=self.REVISION_NOTE,
            )

    def test_second_hook_touching_patch_is_silently_skipped_not_fatal(self) -> None:
        # Only one audit-verified hook word-fix is allowed per repair. A second
        # patch that also touches the hook is no longer fatal to the whole
        # response (Run #30: a batch's one bad patch used to discard every
        # good patch alongside it) - it is simply not applied, and the first,
        # valid hook fix still goes through.
        note = (
            self.REVISION_NOTE
            + "\n- [tone] Also unnatural: 'واحد' placement in narration s1."
        )
        script = json.loads(json.dumps(self.ORIGINAL, ensure_ascii=False))
        repaired = _validate_and_apply_script_patches(
            {
                "patches": [
                    {"section_id": "s1", "find": "أستكين", "replace": "أتجمّد"},
                    {"section_id": "s1", "find": "واحد", "replace": "معيّن"},
                ]
            },
            plan=self.PLAN,
            original_script=script,
            identity=self.IDENTITY,
            cta_plan=self.CTA_PLAN,
            revision_note=note,
        )
        narration = repaired["sections"][0]["narration"]
        self.assertIn("أتجمّد", narration)
        self.assertNotIn("معيّن", narration)

    def test_run18_whole_script_and_hook_typo_flags_get_a_real_accepted_repair(
        self,
    ) -> None:
        tone_block = {
            "status": "block",
            "validation": "valid",
            "preachiness_flags": [],
            "naturalness_flags": [
                "Unnatural Arabic phrase 'أستكين' in narration s1; likely a typo or "
                "non-standard verb."
            ],
            "narrative_format_flags": [
                "editorial_promise_continuity: narrative format mismatch: inner_dialogue "
                "used but content is monologue."
            ],
            "cultural_dignity_flags": [],
            "unverified_religious_quote_flags": [],
            "notes": [],
        }
        brief = {
            "approved_by_user": True,
            "approved_topic": "كيف تنهض عندما تفقد الدافع تمامًا؟",
            "format": "short",
            "language": "ar",
            "audience": "Arabic-speaking adults",
            "editorial_intent": "نبرة هادئة وطبيعية.",
            "research_pack": [],
            "hard_constraints": ["No fabricated facts."],
        }

        class ShortRouter:
            def __init__(self):
                self.calls = 0

            def route(self, *, stage, prompt, max_tokens, validator):
                self.calls += 1
                self_outer.assertEqual(stage, "script_patch")
                return validator(
                    {
                        "patches": [
                            {"section_id": "s1", "find": "أستكين", "replace": "أتجمّد"},
                        ]
                    }
                )

        self_outer = self
        router = ShortRouter()
        audit_calls = {"n": 0}

        def text_audit(**_kwargs):
            audit_calls["n"] += 1
            if audit_calls["n"] == 1:
                raise CleanV2ToneContentBlock(tone_block)
            return {
                "schema_version": 1,
                "status": "pass",
                "factuality_status": "pass",
                "tone_naturalness_status": "pass",
            }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "narrative-identity.json").write_text(
                json.dumps(self.IDENTITY, ensure_ascii=False), encoding="utf-8"
            )
            (root / "cta-plan.json").write_text(
                json.dumps(
                    {**self.CTA_PLAN, "visual_only": True}, ensure_ascii=False
                ),
                encoding="utf-8",
            )
            (root / "structural-ai-flags.json").write_text(
                json.dumps({"flags": [], "mode": "advisory"}, ensure_ascii=False),
                encoding="utf-8",
            )
            script = json.loads(json.dumps(self.ORIGINAL, ensure_ascii=False))
            result = _run_text_audit_with_one_bounded_tone_repair(
                text_audit=text_audit,
                router=router,
                output_dir=root,
                brief=brief,
                plan=self.PLAN,
                script=script,
            )

            self.assertEqual(router.calls, 1)
            self.assertEqual(audit_calls["n"], 2)
            self.assertEqual(result["tone_repair_attempts"], 1)
            self.assertEqual(result["post_repair_structural_ai_status"], "pass")
            self.assertEqual(
                script["sections"][0]["narration"],
                "أشعر أنني أتجمّد في مكان واحد بينما أريد أن أتحرك.",
            )


class PrayerSentenceHardLockTests(unittest.TestCase):
    """Run #29: the "approved identity" commit inserts a fixed, host-owned
    prayer sentence into s1's narration (hook -> prayer -> channel
    definition -> topic) and only asked repair models, via soft prompt text,
    not to touch it. A Mistral patch that overlapped that region was only
    rejected because it also happened to overlap the channel-definition text
    aliased to the pre-existing "opener" hard lock - the prayer sentence
    itself had no hard validator protection at all. Give it the same hard
    lock hook/opener/closer/CTA already have.
    """

    PRAYER = PRAYER_SENTENCE
    DEFINITION = "وهنا في نداء اليقظة، نقترب من أفكار الحياة اليومية بوعيٍ أوضح."
    HOOK = "مرّ اليوم كاملًا ولم أبدأ شيئًا."
    PLAN = {
        "title": "كيف تنهض عندما تفقد الدافع؟",
        "promise": "تحول داخلي واحد يقود إلى خطوة صغيرة.",
        "cta": "",
        "sections": [
            {"id": "s1", "heading": "الصوت الداخلي", "purpose": "فتح التوتر", "visual_query_en": "quiet person thinking"},
            {"id": "s2", "heading": "الاحتكاك", "purpose": "إظهار ما يبقي التردد", "visual_query_en": "hands resting beside notebook"},
            {"id": "s3", "heading": "التحول", "purpose": "إنهاء التوتر بفعل واحد", "visual_query_en": "hand writing one word"},
        ],
    }
    IDENTITY = {"opener": DEFINITION, "closer": "", "transitions": []}
    CTA_PLAN = {"anchor_section_id": "", "spoken_text": ""}

    @classmethod
    def _original(cls) -> dict:
        return {
            "title": cls.PLAN["title"],
            "sections": [
                {
                    "id": "s1",
                    "narration": f"{cls.HOOK} {cls.PRAYER} {cls.DEFINITION}",
                },
                {
                    "id": "s2",
                    "narration": "ربما المشكلة ليست في الدافع، بل في أننا نطلب منه أن يكون قويًا من البداية.",
                },
                {"id": "s3", "narration": "ابدأ بشيء صغير لا يتطلب دافعًا."},
            ],
        }

    def test_patch_overlapping_only_the_prayer_sentence_is_rejected(self) -> None:
        script = self._original()
        with self.assertRaisesRegex(ValueError, "script patch changed locked prayer"):
            _validate_and_apply_script_patches(
                {
                    "patches": [
                        {
                            "section_id": "s1",
                            "find": f"{self.HOOK} {self.PRAYER}",
                            "replace": f"{self.HOOK} دعاء آخر مختلف تمامًا.",
                        }
                    ]
                },
                plan=self.PLAN,
                original_script=script,
                identity=self.IDENTITY,
                cta_plan=self.CTA_PLAN,
                revision_note="- [tone] s1: awkward transition noted",
            )

    def test_patch_untouched_by_prayer_still_succeeds(self) -> None:
        script = self._original()
        repaired = _validate_and_apply_script_patches(
            {
                "patches": [
                    {
                        "section_id": "s2",
                        "find": "قويًا من البداية",
                        "replace": "قويًا منذ اللحظة الأولى",
                    }
                ]
            },
            plan=self.PLAN,
            original_script=script,
            identity=self.IDENTITY,
            cta_plan=self.CTA_PLAN,
            revision_note="- [tone] s2: awkward phrase noted",
        )
        self.assertIn("قويًا منذ اللحظة الأولى", repaired["sections"][1]["narration"])
        self.assertIn(self.PRAYER, repaired["sections"][0]["narration"])

    def test_scripts_without_the_prayer_sentence_are_unaffected(self) -> None:
        script = self._original()
        script["sections"][0]["narration"] = self.HOOK
        repaired = _validate_and_apply_script_patches(
            {
                "patches": [
                    {"section_id": "s2", "find": "المشكلة", "replace": "الصعوبة"}
                ]
            },
            plan=self.PLAN,
            original_script=script,
            identity={"opener": "", "closer": "", "transitions": []},
            cta_plan=self.CTA_PLAN,
            revision_note="- [tone] s2: word choice noted",
        )
        self.assertIn("الصعوبة", repaired["sections"][1]["narration"])


    def test_tone_audit_judges_content_without_host_owned_prayer_or_identity(self) -> None:
        script = self._original()
        brief = {
            "approved_by_user": True,
            "approved_topic": "كيف تنهض عندما تفقد الدافع؟",
            "format": "short",
            "language": "ar",
            "audience": "Arabic-speaking adults",
            "editorial_intent": "نبرة هادئة وطبيعية.",
            "research_pack": [],
            "hard_constraints": ["No fabricated facts."],
        }
        captured: dict[str, object] = {}

        def fake_build(*, brief, plan, script):
            del brief, plan
            built = SimpleNamespace(
                sections=[
                    SimpleNamespace(narration=str(item.get("narration") or ""))
                    for item in script["sections"]
                ],
                hook="",
                closing_payoff="",
                identity_opener="",
                identity_closer="",
                identity_transitions=[],
            )
            captured["audit_script"] = json.loads(
                json.dumps(script, ensure_ascii=False)
            )
            return built

        def fake_tone(_api_key, production_plan, _model):
            captured["plan"] = production_plan
            return {
                "status": "pass",
                "validation": "valid",
                "attempts": [{"provider": "fixture", "outcome": "success"}],
                "preachiness_flags": [],
                "naturalness_flags": [],
                "narrative_format_flags": [],
                "unverified_religious_quote_flags": [],
                "hook_specificity": True,
                "hook_honesty": True,
                "hook_curiosity": True,
                "hook_genericness": False,
            }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "narrative-identity.json").write_text(
                json.dumps(self.IDENTITY, ensure_ascii=False),
                encoding="utf-8",
            )
            with (
                mock.patch(
                    "clean_v2.pipeline._build_production_plan_for_audit",
                    side_effect=fake_build,
                ),
                mock.patch(
                    "clean_v2.tone_audit.audit_tone_and_naturalness_with_mistral",
                    side_effect=fake_tone,
                ),
            ):
                report = _run_legacy_tone_naturalness_audit(
                    output_dir=root,
                    brief=brief,
                    plan=self.PLAN,
                    script=script,
                )

        audited = captured["plan"]
        joined = "\n".join(section.narration for section in audited.sections)
        self.assertNotIn(self.PRAYER, joined)
        self.assertNotIn(self.DEFINITION, joined)
        self.assertEqual(audited.hook, self.HOOK)
        self.assertEqual(audited.identity_opener, self.DEFINITION)
        self.assertTrue(report["trusted_identity_excluded_from_model_judgment"])
        self.assertEqual(
            set(report["trusted_identity"]),
            {self.PRAYER, self.DEFINITION},
        )


class MixedValidityPatchBatchTests(unittest.TestCase):
    """Run #30: Mistral proposed three patches in one response - one correctly
    scoped to the only flagged section (s1), two targeting unflagged sections
    (s2, s3). The whole candidate was rejected ("targeted an unflagged
    section"), discarding the valid s1 fix along with the two invalid ones,
    and the repair attempt was wasted entirely. Each patch must now be
    judged - and applied or skipped - on its own merits.
    """

    PLAN = {
        "title": "كيف تنهض عندما تفقد الدافع؟",
        "promise": "تحول داخلي واحد يقود إلى خطوة صغيرة.",
        "cta": "",
        "sections": [
            {"id": "s1", "heading": "الصوت الداخلي", "purpose": "فتح التوتر", "visual_query_en": "quiet person thinking"},
            {"id": "s2", "heading": "الاحتكاك", "purpose": "إظهار ما يبقي التردد", "visual_query_en": "hands resting beside notebook"},
            {"id": "s3", "heading": "التحول", "purpose": "إنهاء التوتر بفعل واحد", "visual_query_en": "hand writing one word"},
        ],
    }
    ORIGINAL = {
        "title": PLAN["title"],
        "sections": [
            {"id": "s1", "narration": "أحسّيت أن الدافع يختفي كلما احتجته فعلًا في يومي."},
            {"id": "s2", "narration": "ربما المشكلة ليست في الدافع، بل في أننا نطلب منه أن يكون قويًا."},
            {"id": "s3", "narration": "ابدأ بشيء صغير لا يتطلب دافعًا كبيرًا."},
        ],
    }
    IDENTITY = {"opener": "", "closer": "", "transitions": []}
    CTA_PLAN = {"anchor_section_id": "", "spoken_text": ""}
    REVISION_NOTE = (
        "- [tone] Use of colloquial Arabic 'أحسّيت' instead of MSA 'شعرت' in narration s1."
    )

    def test_valid_patch_survives_when_batched_with_unflagged_section_patches(self) -> None:
        script = json.loads(json.dumps(self.ORIGINAL, ensure_ascii=False))
        repaired = _validate_and_apply_script_patches(
            {
                "patches": [
                    {"section_id": "s1", "find": "أحسّيت", "replace": "شعرت"},
                    {"section_id": "s2", "find": "قويًا", "replace": "قويًا منذ البداية"},
                    {"section_id": "s3", "find": "كبيرًا", "replace": "ضخمًا"},
                ]
            },
            plan=self.PLAN,
            original_script=script,
            identity=self.IDENTITY,
            cta_plan=self.CTA_PLAN,
            revision_note=self.REVISION_NOTE,
        )
        self.assertIn("شعرت", repaired["sections"][0]["narration"])
        self.assertNotIn("أحسّيت", repaired["sections"][0]["narration"])
        # Unflagged sections are untouched - neither out-of-scope patch applied.
        self.assertEqual(
            repaired["sections"][1]["narration"], self.ORIGINAL["sections"][1]["narration"]
        )
        self.assertEqual(
            repaired["sections"][2]["narration"], self.ORIGINAL["sections"][2]["narration"]
        )

    def test_batch_with_no_valid_patches_still_raises_with_specific_reason(self) -> None:
        script = json.loads(json.dumps(self.ORIGINAL, ensure_ascii=False))
        with self.assertRaisesRegex(ValueError, "targeted an unflagged section"):
            _validate_and_apply_script_patches(
                {
                    "patches": [
                        {"section_id": "s2", "find": "قويًا", "replace": "قويًا منذ البداية"},
                        {"section_id": "s3", "find": "كبيرًا", "replace": "ضخمًا"},
                    ]
                },
                plan=self.PLAN,
                original_script=script,
                identity=self.IDENTITY,
                cta_plan=self.CTA_PLAN,
                revision_note=self.REVISION_NOTE,
            )


class ShortFactualitySectionTargetRegressionTests(unittest.TestCase):
    def test_cohort_attempt1_section_s2_note_resolves_exact_target(self) -> None:
        from clean_v2.pipeline import _factuality_location_issue_notes

        report = {
            "status": "block",
            "unsupported_claims": [{"section_id": "s2", "issue": "claim that expectations influence emotions more than energy"}],
            "diagnostics": {"raw_result": {"unsupported_claims": [{"section_id": "s2", "issue": "claim that expectations influence emotions more than energy"}], "professional_advice_flags": [], "expert_persona_flags": []}},
            "professional_advice_flags": [],
            "expert_persona_flags": [],
            "notes": ["Section s2 contains an unsupported psychological claim."],
        }
        script = {
            "sections": [
                {"id": "s1", "narration": "أحياناً يختفي الدافع كأنك في منتصف نهارٍ صامت."},
                {"id": "s2", "narration": "لكن الحقيقة أن توقعاتنا للنتيجة تُشغِّل مشاعرنا أكثر من الطاقة نفسها."},
                {"id": "s3", "narration": "ابدأ بتدوين هدف صغير اليوم، ثم اكتب خطوة واحدة لتحقيقه."},
            ]
        }

        notes = _factuality_location_issue_notes(report, script)
        self.assertEqual(notes, "- [factuality-location] s2")
        self.assertEqual(_factuality_target_section_ids(report, script), ("s2",))


if __name__ == "__main__":
    unittest.main()
