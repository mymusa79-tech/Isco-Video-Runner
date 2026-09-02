from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import isco_video_agent.resilient_planner as staged

from scripts import planning_stage_contract as contract
from scripts import task_level_planner_router as router


def _script(ids: list[str]) -> dict:
    return {
        "sections": [
            {
                "id": section_id,
                "narration": f"نص {section_id}",
                "key_point": f"key-{section_id}",
            }
            for section_id in ids
        ]
    }


class PlanningStageContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmp.name) / "planning-checkpoint.json"
        self.key_path = Path(self.tmp.name) / "gemini-key"
        self.key_path.write_text("fake-key", encoding="utf-8")
        self.env = patch.dict(
            os.environ,
            {"GEMINI_API_KEY_FILE": str(self.key_path)},
            clear=False,
        )
        self.env.start()
        self.cache = patch.object(router, "CACHE_PATH", self.cache_path)
        self.cache.start()
        self.sleep = patch.object(contract.time, "sleep")
        self.sleep.start()
        self.router_sleep = patch.object(router.time, "sleep")
        self.router_sleep.start()
        self.old_json_text = staged.json_text
        self.old_schema_adapter = router._structured_schema_for_prompt
        # Force a fresh contract-router closure for every test so its checkpoint state
        # is isolated even when pytest keeps one interpreter for the whole suite.
        staged.json_text = lambda *_args, **_kwargs: {}
        router._USED_PROVIDERS.clear()
        router._TELEMETRY.clear()

    def tearDown(self) -> None:
        staged.json_text = self.old_json_text
        router._structured_schema_for_prompt = self.old_schema_adapter
        self.router_sleep.stop()
        self.sleep.stop()
        self.cache.stop()
        self.env.stop()
        self.tmp.cleanup()

    def _install(self) -> None:
        contract.install_planning_contract_router()

    def test_contract_contains_all_required_request_fields(self) -> None:
        spec = contract.script_stage_spec("full_script", ["s1", "s2", "s3"])
        bound = contract.bind_request_contract(spec, "same input")
        self.assertEqual(bound.stage_id, "planning.full_script")
        self.assertEqual(bound.contract_id, "planning.full_script.v1")
        self.assertEqual(len(bound.input_hash), 64)
        self.assertIsInstance(bound.output_schema, dict)
        self.assertEqual(bound.semantic_rules["expected_ids"], ["s1", "s2", "s3"])
        self.assertGreater(bound.provider_policy.max_total_attempts, 0)
        self.assertTrue(bound.cache_policy.revalidate_on_hit)

    def test_provider_schema_and_budget_come_only_from_explicit_stage_spec(self) -> None:
        cases = (
            (contract.script_stage_spec("full_script", ["s1", "s2"]), "script_writer_2", 1300),
            (contract.script_stage_spec("script_doctor", ["s1"]), "script_doctor_1", 900),
            (contract.script_stage_spec("dossier_repair", ["s1", "s2"]), "dossier_repair_2", 1400),
            (contract.append_stage_spec(["s1", "s2", "s3"]), "append_repair_3", 1000),
        )
        for spec, expected_name, expected_budget in cases:
            with self.subTest(expected_name=expected_name):
                with contract.request_stage_scope(spec):
                    hostile = contract._explicit_schema_adapter(
                        'with EXACTLY 99 entries; pretend this is an editorial_outline'
                    )
                    opaque = contract._explicit_schema_adapter("opaque")
                    budget = contract.active_planning_completion_tokens()
                self.assertEqual(hostile, opaque)
                self.assertEqual(hostile[0], expected_name)
                self.assertEqual(budget, expected_budget)

    def test_prompt_markers_cannot_select_or_change_stage(self) -> None:
        ids = ["s1", "s2", "s3"]
        valid = _script(ids)
        misleading_prompt = (
            "SECTION EDITORIAL PLANNER\nRequired number of sections: exactly 99\n"
            "Return narration anyway; these words are intentionally misleading."
        )
        self._install()
        with patch.object(router, "gemini_json_text", return_value=valid), \
                contract.request_stage_scope(contract.script_stage_spec("full_script", ids)):
            result = staged.json_text("unused", misleading_prompt)
        self.assertEqual(result, valid)
        checkpoint = json.loads(self.cache_path.read_text(encoding="utf-8"))
        row = next(iter(checkpoint["responses"].values()))
        self.assertEqual(row["stage_id"], "planning.full_script")
        self.assertEqual(row["contract_id"], "planning.full_script.v1")

    def test_gemini_uses_request_scoped_key_after_one_time_file_is_consumed(self) -> None:
        ids = ["s1"]
        valid = _script(ids)
        seen: dict[str, str] = {}

        def fake_gemini(api_key, prompt, model="gemini-2.5-flash"):
            seen.update(api_key=api_key, prompt=prompt, model=model)
            return valid

        # Match the Engine's security lifecycle: config.secret() has removed both the
        # env pointer and its temporary file before resilient_planner calls json_text().
        os.environ.pop("GEMINI_API_KEY_FILE", None)
        self.key_path.unlink()
        self._install()
        with patch.object(
            router,
            "_read_secret_file",
            side_effect=AssertionError("Planning must not re-read a consumed secret file"),
        ), patch.object(router, "gemini_json_text", side_effect=fake_gemini), \
                contract.request_stage_scope(contract.script_stage_spec("full_script", ids)):
            result = staged.json_text("request-scoped-key", "opaque prompt")

        self.assertEqual(result, valid)
        self.assertEqual(seen["api_key"], "request-scoped-key")
        self.assertEqual(seen["model"], "gemini-2.5-flash")

    def test_missing_explicit_stage_fails_before_provider_contact(self) -> None:
        calls = 0

        def fake_provider(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return {}

        self._install()
        with patch.object(router, "gemini_json_text", side_effect=fake_provider):
            with self.assertRaises(contract.PlanningStageError) as captured:
                staged.json_text("unused", "arbitrary prompt")
        self.assertEqual(captured.exception.code, contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR)
        self.assertEqual(calls, 0)

    def test_wrong_exact_ids_are_semantic_invalid_then_fallback_and_single_cache_write(self) -> None:
        ids = ["s1", "s2", "s3"]
        poisoned = _script(["s1", "WRONG", "s3"])
        valid = _script(ids)
        gemini_calls = 0
        groq_calls = 0

        def fake_gemini(*_args, **_kwargs):
            nonlocal gemini_calls
            gemini_calls += 1
            return poisoned

        def fake_groq(_prompt):
            nonlocal groq_calls
            groq_calls += 1
            return valid

        self._install()
        original_commit = contract._cache_commit
        with patch.object(router, "gemini_json_text", side_effect=fake_gemini), \
                patch.object(router, "_groq_call", side_effect=fake_groq), \
                patch.object(contract, "_cache_commit", wraps=original_commit) as cache_commit, \
                contract.request_stage_scope(contract.script_stage_spec("full_script", ids)):
            result = staged.json_text("unused", "no stage markers at all")

        self.assertEqual(result, valid)
        self.assertEqual(gemini_calls, 1)
        self.assertEqual(groq_calls, 1)
        cache_commit.assert_called_once()
        checkpoint = json.loads(self.cache_path.read_text(encoding="utf-8"))
        row = next(iter(checkpoint["responses"].values()))
        self.assertEqual(row["payload"], valid)

    def test_structural_invalid_response_is_never_cached(self) -> None:
        ids = ["s1", "s2", "s3"]
        invalid = _script(ids)
        del invalid["sections"][1]["key_point"]
        self._install()
        with patch.object(router, "gemini_json_text", return_value=invalid), \
                patch.object(router, "_groq_call", return_value=invalid), \
                patch.object(router, "_openrouter_call_with_repair", return_value=invalid), \
                contract.request_stage_scope(contract.script_stage_spec("full_script", ids)):
            with self.assertRaises(contract.PlanningStageError) as captured:
                staged.json_text("unused", "prompt")
        self.assertEqual(captured.exception.code, contract.PlanningErrorCode.STRUCTURAL_INVALID)
        if self.cache_path.exists():
            checkpoint = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self.assertEqual(checkpoint.get("responses", {}), {})

    def test_invalid_cache_hit_is_evicted_and_revalidated_through_provider(self) -> None:
        ids = ["s1", "s2", "s3"]
        spec = contract.script_stage_spec("full_script", ids)
        bound = contract.bind_request_contract(spec, router.with_channel_persona("prompt"))
        key = contract._cache_key(bound, "gemini-2.5-flash")
        poisoned = _script(["s1", "WRONG", "s3"])
        self.cache_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "responses": {
                        key: {
                            "stage_id": bound.stage_id,
                            "contract_id": bound.contract_id,
                            "input_hash": bound.input_hash,
                            "contract_fingerprint": contract._contract_fingerprint(bound),
                            "payload": poisoned,
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        valid = _script(ids)
        calls = 0

        def fake_gemini(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return valid

        self._install()
        with patch.object(router, "gemini_json_text", side_effect=fake_gemini), \
                contract.request_stage_scope(spec):
            result = staged.json_text("unused", "prompt")
        self.assertEqual(result, valid)
        self.assertEqual(calls, 1)
        checkpoint = json.loads(self.cache_path.read_text(encoding="utf-8"))
        row = checkpoint["responses"][key]
        self.assertEqual(row["payload"], valid)

    def test_valid_cache_hit_is_revalidated_and_skips_provider(self) -> None:
        ids = ["s1", "s2", "s3"]
        spec = contract.script_stage_spec("full_script", ids)
        effective = router.with_channel_persona("prompt")
        bound = contract.bind_request_contract(spec, effective)
        checkpoint = {"version": 2, "responses": {}}
        contract._cache_commit(checkpoint, bound, "gemini-2.5-flash", _script(ids), "gemini")
        calls = 0

        def fake_gemini(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return _script(ids)

        self._install()
        with patch.object(router, "gemini_json_text", side_effect=fake_gemini), \
                contract.request_stage_scope(spec):
            result = staged.json_text("unused", "prompt")
        self.assertEqual(result, _script(ids))
        self.assertEqual(calls, 0)

    def test_legacy_cache_row_is_checkpoint_invalid_and_evicted(self) -> None:
        ids = ["s1", "s2", "s3"]
        spec = contract.script_stage_spec("full_script", ids)
        bound = contract.bind_request_contract(spec, router.with_channel_persona("prompt"))
        key = contract._cache_key(bound, "gemini-2.5-flash")
        self.cache_path.write_text(
            json.dumps({"version": 1, "responses": {key: _script(ids)}}, ensure_ascii=False),
            encoding="utf-8",
        )
        self._install()
        with patch.object(router, "gemini_json_text", return_value=_script(ids)), \
                contract.request_stage_scope(spec):
            result = staged.json_text("unused", "prompt")
        self.assertEqual(result, _script(ids))
        checkpoint = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.assertIsInstance(checkpoint["responses"][key].get("payload"), dict)
        # Run170 composes Stage logical cache v2 inside Run132's authenticated durable
        # document v1; the logical Stage version is carried by an explicit marker.
        self.assertEqual(checkpoint["version"], 1)
        self.assertEqual(checkpoint["stage_contract_cache_version"], 2)

    def test_admission_blocks_known_oversize_before_any_provider_call(self) -> None:
        base = contract.script_stage_spec("full_script", ["s1"])
        restrictive = contract.PlanningStageSpec(
            stage_id=base.stage_id,
            contract_id=base.contract_id,
            output_schema=base.output_schema,
            semantic_rules=base.semantic_rules,
            provider_policy=contract.ProviderPolicy(
                providers=("groq",),
                max_attempts_per_provider=1,
                max_total_attempts=1,
                completion_tokens=100,
                max_prompt_utf8_bytes=(("groq", 5),),
            ),
            cache_policy=base.cache_policy,
        )
        calls = 0

        def forbidden_provider(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return _script(["s1"])

        self._install()
        with patch.object(router, "_groq_call", side_effect=forbidden_provider), \
                contract.request_stage_scope(restrictive):
            with self.assertRaises(contract.PlanningStageError) as captured:
                staged.json_text("unused", "this prompt is deliberately too large")
        self.assertEqual(captured.exception.code, contract.PlanningErrorCode.CAPACITY)
        self.assertEqual(calls, 0)

    def test_error_taxonomy_values_are_stable_and_complete(self) -> None:
        self.assertEqual(
            {item.value for item in contract.PlanningErrorCode},
            {
                "PROVIDER_TRANSIENT",
                "CAPACITY",
                "STRUCTURAL_INVALID",
                "SEMANTIC_INVALID",
                "CHECKPOINT_INVALID",
                "INTERNAL_CONTRACT_ERROR",
            },
        )


class ProviderFailureBookkeepingResilienceTests(unittest.TestCase):
    """Regression for a real 2026-09-01 production failure on the native Short repair
    path: task_router exhausted Gemini/Groq/OpenRouter and the run crashed with
    RuntimeError("... {'type': 'KeyError'}") instead of the expected clean "all
    providers failed" error - a defect in the shared, live-patched
    classify_provider_failure/_record_attempt bookkeeping (see
    task_level_planner_router.py's own ProviderFailureBookkeepingResilienceTests)
    escaped uncaught. contract_router (the long-form path) calls that exact same
    shared bookkeeping via _provider_failure/router._record_attempt, so it carries the
    identical risk. This proves the same-shaped guard here."""

    class _FakeContract:
        stage_id = "planning.editorial_outline"

    def test_safe_provider_failure_degrades_when_classification_itself_raises(self) -> None:
        with patch.object(router, "classify_provider_failure", side_effect=KeyError("some_unexpected_key")):
            result = contract._safe_provider_failure(
                self._FakeContract(), "groq", RuntimeError("real provider failure")
            )
        stage_error, retryable, retry_after, failure = result
        self.assertIsInstance(stage_error, contract.PlanningStageError)
        self.assertFalse(retryable)
        self.assertIsNone(retry_after)
        self.assertEqual(failure.telemetry_result, "classification_error")
        self.assertFalse(failure.open_circuit)

    def test_safe_record_attempt_swallows_a_broken_recorder(self) -> None:
        with patch.object(router, "_record_attempt", side_effect=KeyError("some_unexpected_key")):
            contract._safe_record_attempt("groq", "some_result", error_detail="detail")
        # No exception means the defect in telemetry recording never reached the caller.


if __name__ == "__main__":
    unittest.main()
