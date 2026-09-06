from __future__ import annotations

import importlib
import unittest
from unittest import mock

from scripts import planning_stage_contract as stage_contract
from scripts import task_level_planner_router as router
from scripts import run125_capacity_routing_closure as run125
import isco_video_agent.resilient_planner as staged


class PlanningOutlineSplitSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        from scripts import planning_outline_split_contract as split

        self.split = importlib.reload(split)
        self.split._TERMINAL_REQUEST_FINGERPRINTS.clear()

    def test_core_contract_excludes_section_briefs(self) -> None:
        schema = self.split.outline_core_schema(6)
        self.assertNotIn("section_briefs", schema["properties"])
        self.assertNotIn("section_briefs", schema["required"])
        self.assertIn("editorial_intent", schema["required"])
        self.assertFalse(schema["additionalProperties"])

    def test_sections_contract_contains_only_exact_section_briefs(self) -> None:
        schema = self.split.outline_sections_schema(6)
        self.assertEqual(set(schema["properties"]), {"section_briefs"})
        self.assertEqual(schema["required"], ["section_briefs"])
        briefs = schema["properties"]["section_briefs"]
        self.assertEqual(briefs["minItems"], 6)
        self.assertEqual(briefs["maxItems"], 6)
        self.assertFalse(schema["additionalProperties"])

    def test_split_specs_keep_existing_bounded_transport_budget(self) -> None:
        core = self.split.outline_core_stage_spec(6)
        sections = self.split.outline_sections_stage_spec(6)
        self.assertEqual(
            core.provider_policy.completion_tokens,
            stage_contract.OUTLINE_COMPLETION_TOKEN_BUDGET,
        )
        self.assertEqual(
            sections.provider_policy.completion_tokens,
            stage_contract.OUTLINE_COMPLETION_TOKEN_BUDGET,
        )
        self.assertNotEqual(core.contract_id, sections.contract_id)
        self.assertEqual(core.semantic_rules["transport_profile"], self.split.CORE_PROFILE)
        self.assertEqual(
            sections.semantic_rules["transport_profile"], self.split.SECTIONS_PROFILE
        )

    def test_sections_validation_rejects_wrong_count_and_duplicate_ids(self) -> None:
        spec = self.split.outline_sections_stage_spec(2)
        contract = stage_contract.bind_request_contract(spec, "sections-request")
        one = {
            "section_briefs": [
                {
                    "id": "s1",
                    "purpose": "one",
                    "visual_query": "quiet room",
                    "on_screen_text": "one",
                    "emotion": "calm",
                    "expected_seconds": 10,
                }
            ]
        }
        with self.assertRaises(stage_contract.PlanningStageError) as caught:
            self.split._validate_sections(one, contract)
        self.assertEqual(caught.exception.code, stage_contract.PlanningErrorCode.STRUCTURAL_INVALID)

        duplicate = {
            "section_briefs": [
                {
                    "id": "s1",
                    "purpose": "one",
                    "visual_query": "quiet room",
                    "on_screen_text": "one",
                    "emotion": "calm",
                    "expected_seconds": 10,
                },
                {
                    "id": "s1",
                    "purpose": "two",
                    "visual_query": "open road",
                    "on_screen_text": "two",
                    "emotion": "hopeful",
                    "expected_seconds": 10,
                },
            ]
        }
        with self.assertRaises(stage_contract.PlanningStageError) as caught:
            self.split._validate_sections(duplicate, contract)
        self.assertEqual(caught.exception.code, stage_contract.PlanningErrorCode.SEMANTIC_INVALID)

    def test_core_reuses_full_narrative_identity_semantics(self) -> None:
        spec = self.split.outline_core_stage_spec(6)
        contract = stage_contract.bind_request_contract(spec, "core-request")
        core = {
            "pillar": "understand",
            "hook": "hook",
            "title_options": ["a", "b", "c"],
            "thumbnail_concepts": ["a", "b", "c"],
            "cta": "cta",
            "closing_payoff": "payoff",
            "narrative_format": next(iter(staged._NARRATIVE_FORMATS)),
            "opener_variant": "fresh opener",
            "closer_variant": "fresh closer",
            "transition_variants": ["t1", "t2", "t3"],
            "editorial_intent": {
                "editorial_thesis": "thesis",
                "viewer_starting_belief": "belief",
                "hidden_assumption": "assumption",
                "editorial_turn": "turn",
                "stakes": "stakes",
                "viewer_promise": "promise",
                "evidence_boundaries": ["boundary"],
                "earned_payoff": "earned",
            },
        }
        with (
            mock.patch.object(staged, "validate_narrative_format", return_value=[]),
            mock.patch.object(staged, "validate_identity_phrases", return_value=[]),
            mock.patch.object(staged, "intent_from_dict", return_value=object()),
        ):
            self.assertIs(self.split._validate_core(core, contract), core)

        broken = dict(core)
        broken["narrative_format"] = "not-supported"
        with self.assertRaises(stage_contract.PlanningStageError) as caught:
            self.split._validate_core(broken, contract)
        self.assertEqual(caught.exception.code, stage_contract.PlanningErrorCode.SEMANTIC_INVALID)


class PlanningOutlineSplitTopologyTests(unittest.TestCase):
    def setUp(self) -> None:
        from scripts import planning_outline_split_contract as split

        self.split = importlib.reload(split)
        self.original_outline = staged._outline
        self.original_json = staged.json_text
        self.original_validate = stage_contract.validate_response

    def tearDown(self) -> None:
        staged._outline = self.original_outline
        staged.json_text = self.original_json
        stage_contract.validate_response = self.original_validate
        self.split._ACTIVE_OUTLINE_CALLS.set(None)

    @staticmethod
    def _outline_kwargs() -> dict:
        return {
            "topic": "موضوع",
            "fmt": "film",
            "model": "model",
            "policy_json": "{}",
            "research_json": "{}",
            "avoid_json": "{}",
            "learning_json": "{}",
            "revision_note": "",
        }

    def test_engine_two_calls_receive_core_then_sections_contract(self) -> None:
        observed: list[tuple[str, str]] = []

        def fake_json(api_key, prompt, model="model"):
            spec = stage_contract._ACTIVE_STAGE_SPEC.get()
            self.assertIsNotNone(spec)
            observed.append((spec.stage_id, spec.semantic_rules["transport_profile"]))
            return {"which": len(observed)}

        def fake_outline(api_key, **kwargs):
            first = staged.json_text(api_key, "CORE", model=kwargs["model"])
            second = staged.json_text(api_key, "SECTIONS", model=kwargs["model"])
            return {"first": first, "second": second}

        staged.json_text = fake_json
        staged._outline = fake_outline
        stage_contract.validate_response = lambda contract, data: data
        self.split._install_call_sequence_binding()

        # This test isolates Engine-call topology. Canonical-domain validation is
        # exercised separately against the pinned Engine's real _outline() transform.
        with mock.patch.object(
            self.split,
            "_validate_canonical_outline",
            side_effect=lambda data, contract, expected: data,
        ) as canonical_guard:
            result = staged._outline("key", **self._outline_kwargs())
        canonical_guard.assert_called_once()
        self.assertEqual(result["first"], {"which": 1})
        self.assertEqual(result["second"], {"which": 2})
        self.assertEqual(
            observed,
            [
                ("planning.editorial_outline_core", self.split.CORE_PROFILE),
                ("planning.editorial_outline_sections", self.split.SECTIONS_PROFILE),
            ],
        )

    def test_engine_third_outline_model_call_fails_closed(self) -> None:
        def fake_json(api_key, prompt, model="model"):
            return {"ok": True}

        def fake_outline(api_key, **kwargs):
            staged.json_text(api_key, "CORE", model=kwargs["model"])
            staged.json_text(api_key, "SECTIONS", model=kwargs["model"])
            staged.json_text(api_key, "UNEXPECTED", model=kwargs["model"])
            return {"ok": True}

        staged.json_text = fake_json
        staged._outline = fake_outline
        stage_contract.validate_response = lambda contract, data: data
        self.split._install_call_sequence_binding()

        with self.assertRaisesRegex(
            stage_contract.PlanningStageError,
            "unexpected json_text call index=3",
        ):
            staged._outline("key", **self._outline_kwargs())

    def test_engine_one_outline_model_call_cannot_become_plan_authority(self) -> None:
        def fake_json(api_key, prompt, model="model"):
            return {"ok": True}

        def fake_outline(api_key, **kwargs):
            staged.json_text(api_key, "CORE", model=kwargs["model"])
            return {"partial": True}

        staged.json_text = fake_json
        staged._outline = fake_outline
        stage_contract.validate_response = lambda contract, data: data
        self.split._install_call_sequence_binding()

        with self.assertRaisesRegex(
            stage_contract.PlanningStageError,
            "expected=2 actual=1",
        ):
            staged._outline("key", **self._outline_kwargs())

    def test_provider_internal_retries_do_not_advance_engine_call_index(self) -> None:
        observed: list[str] = []

        def fake_json(api_key, prompt, model="model"):
            # Simulate multiple provider-level contacts beneath ONE Engine json_text call.
            spec = stage_contract._ACTIVE_STAGE_SPEC.get()
            observed.extend([spec.stage_id, spec.stage_id, spec.stage_id])
            return {"ok": True}

        def fake_outline(api_key, **kwargs):
            staged.json_text(api_key, "CORE", model=kwargs["model"])
            staged.json_text(api_key, "SECTIONS", model=kwargs["model"])
            return {"assembled": True}

        staged.json_text = fake_json
        staged._outline = fake_outline
        stage_contract.validate_response = lambda contract, data: data
        self.split._install_call_sequence_binding()
        with mock.patch.object(
            self.split,
            "_validate_canonical_outline",
            side_effect=lambda data, contract, expected: data,
        ) as canonical_guard:
            staged._outline("key", **self._outline_kwargs())
        canonical_guard.assert_called_once()

        self.assertEqual(
            observed,
            ["planning.editorial_outline_core"] * 3
            + ["planning.editorial_outline_sections"] * 3,
        )


class PlanningOutlineSplitFailureAwareRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        from scripts import planning_outline_split_contract as split

        self.split = importlib.reload(split)
        self.split._TERMINAL_REQUEST_FINGERPRINTS.clear()
        self.original_gemini = router.gemini_json_text
        self.original_groq = router._groq_call
        self.original_unavailable = run125._is_model_unavailable
        self.had_fingerprint_marker = hasattr(router, "_ISCO_OUTLINE_SPLIT_FINGERPRINT_GUARD")
        self.old_fingerprint_marker = getattr(
            router, "_ISCO_OUTLINE_SPLIT_FINGERPRINT_GUARD", None
        )
        self.had_model_marker = hasattr(run125, "_ISCO_OUTLINE_SPLIT_SCHEMA_MODEL_FAILOVER")
        self.old_model_marker = getattr(
            run125, "_ISCO_OUTLINE_SPLIT_SCHEMA_MODEL_FAILOVER", None
        )
        if self.had_fingerprint_marker:
            delattr(router, "_ISCO_OUTLINE_SPLIT_FINGERPRINT_GUARD")
        if self.had_model_marker:
            delattr(run125, "_ISCO_OUTLINE_SPLIT_SCHEMA_MODEL_FAILOVER")

    def tearDown(self) -> None:
        router.gemini_json_text = self.original_gemini
        router._groq_call = self.original_groq
        run125._is_model_unavailable = self.original_unavailable
        if self.had_fingerprint_marker:
            setattr(
                router,
                "_ISCO_OUTLINE_SPLIT_FINGERPRINT_GUARD",
                self.old_fingerprint_marker,
            )
        elif hasattr(router, "_ISCO_OUTLINE_SPLIT_FINGERPRINT_GUARD"):
            delattr(router, "_ISCO_OUTLINE_SPLIT_FINGERPRINT_GUARD")
        if self.had_model_marker:
            setattr(
                run125,
                "_ISCO_OUTLINE_SPLIT_SCHEMA_MODEL_FAILOVER",
                self.old_model_marker,
            )
        elif hasattr(run125, "_ISCO_OUTLINE_SPLIT_SCHEMA_MODEL_FAILOVER"):
            delattr(run125, "_ISCO_OUTLINE_SPLIT_SCHEMA_MODEL_FAILOVER")
        stage_contract._ACTIVE_REQUEST_CONTRACT.set(None)
        stage_contract._ACTIVE_STAGE_SPEC.set(None)

    def test_gemini_truncation_blocks_same_exact_split_fingerprint_without_second_http_call(self) -> None:
        calls = 0

        def truncating_gemini(*args, **kwargs):
            nonlocal calls
            calls += 1
            raise RuntimeError("GEMINI_INTERACTION_OUTPUT_TRUNCATED")

        router.gemini_json_text = truncating_gemini
        router._groq_call = lambda prompt: {"ok": True}
        self.split._install_same_fingerprint_guard()

        spec = self.split.outline_core_stage_spec(6)
        contract = stage_contract.bind_request_contract(spec, "same-core-request")
        token = stage_contract._ACTIVE_REQUEST_CONTRACT.set(contract)
        try:
            with self.assertRaisesRegex(RuntimeError, "OUTPUT_TRUNCATED"):
                router.gemini_json_text("key", "prompt", model="model")
            with self.assertRaises(stage_contract.PlanningStageError) as caught:
                router.gemini_json_text("key", "prompt", model="model")
        finally:
            stage_contract._ACTIVE_REQUEST_CONTRACT.reset(token)

        self.assertEqual(calls, 1)
        self.assertEqual(caught.exception.code, stage_contract.PlanningErrorCode.CAPACITY)
        self.assertIn("same_fingerprint_blocked", str(caught.exception))

    def test_groq_schema_failure_is_model_diverse_only_for_split_outline(self) -> None:
        run125._is_model_unavailable = lambda error: False
        self.split._install_groq_model_diversity()
        error = RuntimeError(
            "GROQ_JSON_VALIDATE_FAILED status=400 code=json_validate_failed"
        )

        with stage_contract.request_stage_scope(self.split.outline_sections_stage_spec(6)):
            self.assertTrue(run125._is_model_unavailable(error))
            self.assertFalse(run125._is_model_unavailable(RuntimeError("GROQ_HTTP_500")))

        with stage_contract.request_stage_scope(stage_contract.script_stage_spec("full_script", ["s1"])):
            self.assertFalse(run125._is_model_unavailable(error))


class PlanningSplitGeminiHeadroomTests(unittest.TestCase):
    """Run #209 (identical to #204/#205): Gemini truncated real Film/Long
    editorial_intent content at the shared 2400-token budget, which is sized from
    Groq's own 8000 TPM ceiling and has nothing to do with Gemini's real ceiling.
    """

    def setUp(self) -> None:
        from scripts import planning_outline_split_contract as split

        self.split = importlib.reload(split)
        self.split._TERMINAL_REQUEST_FINGERPRINTS.clear()

    def test_gemini_gets_more_completion_headroom_than_groq_on_both_split_calls(self) -> None:
        for spec in (
            self.split.outline_core_stage_spec(6),
            self.split.outline_sections_stage_spec(6),
        ):
            with self.subTest(stage_id=spec.stage_id):
                policy = spec.provider_policy
                self.assertEqual(policy.completion_tokens, stage_contract.OUTLINE_COMPLETION_TOKEN_BUDGET)
                self.assertGreater(
                    policy.completion_tokens_for("gemini"),
                    policy.completion_tokens,
                )
                self.assertEqual(policy.completion_tokens_for("gemini"), self.split._GEMINI_COMPLETION_TOKENS)
                # Groq's own TPM admission math must never move: it is already
                # razor-thin for Film (Run #208: GROQ_TPM_WINDOW_BUSY_PRECHECK at
                # this exact budget), so only Gemini gets real headroom here.
                self.assertEqual(policy.completion_tokens_for("groq"), stage_contract.OUTLINE_COMPLETION_TOKEN_BUDGET)
                self.assertEqual(
                    policy.completion_tokens_for("openrouter"), stage_contract.OUTLINE_COMPLETION_TOKEN_BUDGET
                )

    def test_gemini_call_actually_receives_the_larger_max_output_tokens(self) -> None:
        spec = self.split.outline_core_stage_spec(6)
        contract = stage_contract.bind_request_contract(spec, "core-request")
        captured: dict = {}

        def fake_gemini(api_key, prompt, model="gemini-2.5-flash", **kwargs):
            captured.update(kwargs)
            return {}

        with mock.patch.object(router, "gemini_json_text", side_effect=fake_gemini):
            stage_contract._provider_result("gemini", "prompt", "model", contract, "key")

        self.assertEqual(captured.get("max_output_tokens"), self.split._GEMINI_COMPLETION_TOKENS)
        self.assertGreater(self.split._GEMINI_COMPLETION_TOKENS, stage_contract.OUTLINE_COMPLETION_TOKEN_BUDGET)

    def test_non_split_stages_are_unaffected_gemini_gets_the_same_shared_budget(self) -> None:
        spec = stage_contract.script_stage_spec("full_script", ["s1"])
        contract = stage_contract.bind_request_contract(spec, "script-request")
        captured: dict = {}

        def fake_gemini(api_key, prompt, model="gemini-2.5-flash", **kwargs):
            captured.update(kwargs)
            return {}

        with mock.patch.object(router, "gemini_json_text", side_effect=fake_gemini):
            stage_contract._provider_result("gemini", "prompt", "model", contract, "key")

        self.assertEqual(captured.get("max_output_tokens"), contract.provider_policy.completion_tokens)

    def test_groq_admission_math_is_untouched_by_the_gemini_override(self) -> None:
        # planning_envelope_preflight.py's Groq TPM sizing must keep reading the base
        # completion_tokens field, never the per-provider override.
        core = self.split.outline_core_stage_spec(6)
        self.assertEqual(core.provider_policy.completion_tokens, stage_contract.OUTLINE_COMPLETION_TOKEN_BUDGET)


class PlanningSplitEnvelopeTests(unittest.TestCase):
    def test_preflight_sizes_both_real_engine_split_prompts(self) -> None:
        from scripts import planning_envelope_preflight as preflight

        with (
            mock.patch.object(preflight, "load_editorial_policy", return_value={}),
            mock.patch.object(preflight, "novelty_context", return_value={}),
            mock.patch.object(preflight, "learning_context", return_value={}),
        ):
            core, sections, core_size, sections_size = preflight._split_outline_envelopes(
                brief={"approved_topic": "كيف تستعيد تركيزك بهدوء؟"},
                fmt="film",
                research={},
            )

        self.assertGreater(core_size, 0)
        self.assertGreater(sections_size, 0)
        self.assertEqual(core["contract"], "editorial_outline_core")
        self.assertEqual(sections["contract"], "editorial_outline_sections")
        self.assertEqual(
            core["reserved_completion_tokens"],
            stage_contract.OUTLINE_COMPLETION_TOKEN_BUDGET,
        )
        self.assertEqual(
            sections["reserved_completion_tokens"],
            stage_contract.OUTLINE_COMPLETION_TOKEN_BUDGET,
        )
        self.assertGreater(core["estimated_request_tokens"], 0)
        self.assertGreater(sections["estimated_request_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
