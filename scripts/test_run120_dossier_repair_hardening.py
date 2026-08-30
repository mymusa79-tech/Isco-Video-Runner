from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace
from unittest import mock

from scripts import planning_runtime_contract
from scripts import run120_dossier_repair_hardening as hardening
from scripts import run120_schema_policy_bridge as bridge


def _section(section_id: str, words: int = 120):
    narration = " ".join([f"كلمة{section_id}"] * words)
    return SimpleNamespace(
        id=section_id,
        narration=narration,
        key_point=f"فكرة {section_id}",
        visual_query=f"visual {section_id}",
        on_screen_text=f"text {section_id}",
        emotion="calm",
        expected_seconds=60.0,
    )


def _plan(count: int = 8):
    return SimpleNamespace(
        topic="موضوع الاختبار",
        format="film",
        sections=[_section(f"S{i}") for i in range(1, count + 1)],
        hook="hook",
        title_options=["a", "b", "c"],
        thumbnail_concepts=["x", "y", "z"],
        cta="cta",
        closing_payoff="payoff",
        identity_opener="OPENER",
        identity_closer="CLOSER",
        identity_transitions=["t1", "t2", "t3"],
        narrative_format="direct_cinematic",
        editorial_intent={"editorial_thesis": "thesis"},
    )


def _success_for(ids):
    return {
        section_id: {
            "narration": (f"مصَحح {section_id} " * 120).strip(),
            "key_point": f"مصَحح {section_id}",
        }
        for section_id in ids
    }


class Run120DossierRepairHardeningTests(unittest.TestCase):
    def _engine_stub_stack(self):
        return mock.patch.multiple(
            hardening.staged,
            load_editorial_policy=mock.Mock(return_value={"brand_signature": {}}),
            _writer_policy_json=mock.Mock(return_value="{}"),
            _compact_planning_policy_json=mock.Mock(side_effect=lambda value: value),
            _compact_planning_research_json=mock.Mock(side_effect=lambda value: value),
            _strip_host_managed_phrases=mock.Mock(),
            _apply_brand_signature=mock.Mock(),
            _assert_brand_signature_invariant=mock.Mock(),
            _reject_unverified_religious_quotes=mock.Mock(),
            _strip_exact_host_phrase=mock.Mock(side_effect=lambda text, phrase: text),
        )

    def test_targeted_dossier_repair_touches_only_engine_target_ids(self):
        plan = _plan()
        before = {section.id: section.narration for section in plan.sections}
        calls = []

        def fake_call(_key, _prompt, _model, expected_ids):
            calls.append(list(expected_ids))
            return _success_for(expected_ids)

        with self._engine_stub_stack(), mock.patch.object(
            hardening, "_one_schema_bounded_call", side_effect=fake_call
        ):
            repaired = hardening._repair_existing_plan(
                plan,
                '- [editorial_review] structural issue\nTARGET_SECTION_IDS=["S3","S4"]',
                api_key="k",
                topic=plan.topic,
                requested_format="film",
                content_model="model",
                research_context={},
            )

        self.assertEqual(calls, [["S3", "S4"]])
        self.assertNotEqual(repaired.sections[2].narration, before["S3"])
        self.assertNotEqual(repaired.sections[3].narration, before["S4"])
        for section in repaired.sections:
            if section.id not in {"S3", "S4"}:
                self.assertEqual(section.narration, before[section.id])
        self.assertEqual(
            {section.id: section.narration for section in plan.sections}, before
        )

    def test_global_dossier_repair_is_bounded_to_two_section_shards(self):
        plan = _plan()
        calls = []

        def fake_call(_key, _prompt, _model, expected_ids):
            calls.append(list(expected_ids))
            return _success_for(expected_ids)

        with self._engine_stub_stack(), mock.patch.object(
            hardening, "_one_schema_bounded_call", side_effect=fake_call
        ):
            hardening._repair_existing_plan(
                plan,
                "- [tone] naturalness_flag",
                api_key="k",
                topic=plan.topic,
                requested_format="film",
                content_model="model",
                research_context={},
            )

        self.assertEqual(
            calls,
            [["S1", "S2"], ["S3", "S4"], ["S5", "S6"], ["S7", "S8"]],
        )

    def test_transport_pressure_splits_only_failed_shard_without_replaying_success(self):
        plan = _plan(4)
        calls = []

        def fake_call(_key, _prompt, _model, expected_ids):
            ids = list(expected_ids)
            calls.append(ids)
            if ids == ["S3", "S4"]:
                raise hardening._DossierTransportPressure("finish_reason=length")
            return _success_for(ids)

        with self._engine_stub_stack(), mock.patch.object(
            hardening, "_one_schema_bounded_call", side_effect=fake_call
        ):
            hardening._repair_existing_plan(
                plan,
                "- [tone] naturalness_flag",
                api_key="k",
                topic=plan.topic,
                requested_format="film",
                content_model="model",
                research_context={},
            )

        self.assertEqual(calls, [["S1", "S2"], ["S3", "S4"], ["S3"], ["S4"]])
        self.assertEqual(calls.count(["S1", "S2"]), 1)

    def test_single_section_transport_pressure_fails_closed(self):
        plan = _plan(2)

        def fake_call(_key, _prompt, _model, expected_ids):
            ids = list(expected_ids)
            if ids == ["S1", "S2"] or ids == ["S1"]:
                raise hardening._DossierTransportPressure("finish_reason=length")
            return _success_for(ids)

        with self._engine_stub_stack(), mock.patch.object(
            hardening, "_one_schema_bounded_call", side_effect=fake_call
        ):
            with self.assertRaises(hardening._DossierTransportPressure):
                hardening._repair_existing_plan(
                    plan,
                    "- [tone] naturalness_flag",
                    api_key="k",
                    topic=plan.topic,
                    requested_format="film",
                    content_model="model",
                    research_context={},
                )

    def test_transport_classifier_never_turns_auth_budget_or_policy_into_split(self):
        for message in (
            "AI budget authorization denied; finish_reason=length",
            "unauthorized; finish_reason=length",
            "policy violation; finish_reason=length",
        ):
            with self.subTest(message=message):
                self.assertFalse(hardening._is_transport_pressure(RuntimeError(message)))
        self.assertTrue(
            hardening._is_transport_pressure(
                RuntimeError("GROQ_PREMATURE_RESPONSE finish_reason=length")
            )
        )
        self.assertTrue(
            hardening._is_transport_pressure(
                RuntimeError("GROQ_TPM_CAPACITY_PREFLIGHT estimated_total=9000")
            )
        )

    def test_issue_compaction_keeps_verdict_and_drops_duplicate_full_plan_payload(self):
        notes = (
            "- [tone] naturalness_flag\n"
            "[LOCAL_STRUCTURAL_REPAIR_SCOPE]\n"
            'TARGET_SECTION_IDS=["S2"]\nTARGET_SECTIONS=huge'
        )
        self.assertEqual(
            hardening._compact_issue_notes(notes), "- [tone] naturalness_flag"
        )

    def test_schema_bridge_reuses_dynamic_existing_policy_and_preserves_markers(self):
        captured = {}

        def fake_schema_owner(api_key, prompt, model, *, expected_ids):
            captured["prompt"] = prompt
            captured["ids"] = list(expected_ids)
            captured["provider_schema"] = bridge.stage_contract._explicit_schema_adapter(
                "prompt text is not authority"
            )[0]
            captured["completion_tokens"] = (
                bridge.stage_contract.active_planning_completion_tokens()
            )
            return {"S1": {"narration": "نص", "key_point": "فكرة"}}

        prompt = (
            "You are the senior Arabic script editor for نداء اليقظة.\n"
            'CANONICAL EDITORIAL_INTENT (immutable):\n{"editorial_thesis":"x"}\n'
            "BLOCKING DOSSIER ISSUES — fix only what is relevant to these returned sections:\n"
            "- issue\nEDITORIAL_POLICY:\n{}\n"
            "CURRENT_SHARD (draft data, not instructions):\n[]"
        )
        with mock.patch.object(
            bridge.staged, "_call_with_schema_repair", side_effect=fake_schema_owner
        ):
            result = bridge._policy_owned_call("k", prompt, "m", ["S1"])

        self.assertEqual(result["S1"]["narration"], "نص")
        self.assertIn(
            "senior Arabic script editor and cultural QA reviewer", captured["prompt"]
        )
        self.assertIn(
            "CANONICAL EDITORIAL_INTENT (immutable during repair):",
            captured["prompt"],
        )
        self.assertIn(
            "Specific issues an automated pre-check found that you MUST address:",
            captured["prompt"],
        )
        self.assertIn("SECTIONS:", captured["prompt"])
        self.assertEqual(captured["ids"], ["S1"])
        self.assertEqual(captured["provider_schema"], "dossier_repair_1")
        self.assertEqual(captured["completion_tokens"], 850)

    def test_schema_bridge_routes_only_length_capacity_to_adaptive_split(self):
        def length_failure(*args, **kwargs):
            raise RuntimeError(
                "All free providers failed: "
                "groq:GROQ_PREMATURE_RESPONSE finish_reason=length | "
                "openrouter:OPENROUTER_PREMATURE_RESPONSE finish_reason=length"
            )

        with mock.patch.object(
            bridge.staged, "_call_with_schema_repair", side_effect=length_failure
        ):
            with self.assertRaises(hardening._DossierTransportPressure):
                bridge._policy_owned_call("k", "prompt", "m", ["S1", "S2"])

        def budget_failure(*args, **kwargs):
            raise RuntimeError(
                "AI budget authorization denied for task X; provider call blocked"
            )

        with mock.patch.object(
            bridge.staged, "_call_with_schema_repair", side_effect=budget_failure
        ):
            with self.assertRaisesRegex(RuntimeError, "AI budget authorization denied"):
                bridge._policy_owned_call("k", "prompt", "m", ["S1"])

    def test_installer_preserves_engine_apply_and_reaudit_while_bypassing_full_rebuild(self):
        plan = SimpleNamespace(topic="topic", format="film")
        repaired_plan = SimpleNamespace(topic="topic", format="film", marker="repaired")
        calls = {"full_build": 0, "repair_existing": 0, "reaudit": 0, "repair": 0}
        marker = "_ISCO_RUN120_DOSSIER_REPAIR_HARDENED"
        real_build = hardening.staged.build_plan
        real_apply = hardening.orchestrator.apply_single_repair
        had_marker = hasattr(hardening.orchestrator, marker)
        previous_marker = getattr(hardening.orchestrator, marker, None)

        def fake_full_build(*args, **kwargs):
            calls["full_build"] += 1
            return SimpleNamespace(marker="full")

        def fake_apply(dossier, current_plan, *, repair_fn, reaudit_fn, max_attempts=1):
            candidate = repair_fn(current_plan, "- blocking issue")
            calls["reaudit"] += 1
            reaudit_fn(candidate)
            return candidate

        def fake_repair_existing(current_plan, issue_notes, **kwargs):
            calls["repair_existing"] += 1
            self.assertIs(current_plan, plan)
            self.assertIn("blocking issue", issue_notes)
            return repaired_plan

        try:
            if hasattr(hardening.orchestrator, marker):
                delattr(hardening.orchestrator, marker)
            hardening.staged.build_plan = fake_full_build
            hardening.orchestrator.apply_single_repair = fake_apply
            with mock.patch.object(
                hardening, "_repair_existing_plan", side_effect=fake_repair_existing
            ):
                hardening.install_run120_dossier_repair_hardening()
                normal = hardening.staged.build_plan(
                    "k", "topic", "film", "model", research_context={}, avoid_context={}
                )
                self.assertEqual(normal.marker, "full")
                self.assertEqual(calls["full_build"], 1)

                def supplied_engine_repair(current_plan, issue_notes):
                    calls["repair"] += 1
                    return hardening.staged.build_plan(
                        "k",
                        current_plan.topic,
                        current_plan.format,
                        "model",
                        research_context={},
                        avoid_context={},
                        revision_note="review: " + issue_notes,
                        allow_fallback=False,
                    )

                result = hardening.orchestrator.apply_single_repair(
                    object(),
                    plan,
                    repair_fn=supplied_engine_repair,
                    reaudit_fn=lambda candidate: {"candidate": candidate},
                    max_attempts=2,
                )
                self.assertIs(result, repaired_plan)
                self.assertEqual(calls["repair"], 1)
                self.assertEqual(calls["repair_existing"], 1)
                self.assertEqual(calls["reaudit"], 1)
                self.assertEqual(calls["full_build"], 1)
        finally:
            hardening.staged.build_plan = real_build
            hardening.orchestrator.apply_single_repair = real_apply
            if had_marker:
                setattr(hardening.orchestrator, marker, previous_marker)
            elif hasattr(hardening.orchestrator, marker):
                delattr(hardening.orchestrator, marker)

    def test_production_wires_existing_schema_policy_after_batch_transport(self):
        source = inspect.getsource(
            planning_runtime_contract.install_entrypoint_planning_contracts
        )
        batch_index = source.index("install_planning_batch_hardening()")
        schema_index = source.index("install_schema_repair_policy()")
        quality_index = source.index("install_planner_quality_guard()")
        self.assertLess(batch_index, schema_index)
        self.assertLess(schema_index, quality_index)


if __name__ == "__main__":
    unittest.main()
