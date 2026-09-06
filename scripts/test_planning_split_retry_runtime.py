from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import isco_video_agent.resilient_planner as staged

from scripts import checkpoint_namespace_guard as checkpoint_guard
from scripts import planning_outline_split_contract as split
from scripts import planning_stage_contract as contract
from scripts import task_level_planner_router as router


def _core_payload() -> dict:
    return {
        "pillar": "understand",
        "hook": "خطاف",
        "title_options": ["أ", "ب", "ج"],
        "thumbnail_concepts": ["a", "b", "c"],
        "cta": "ختام",
        "closing_payoff": "خلاصة",
        "narrative_format": "direct_cinematic",
        "opener_variant": "افتتاح",
        "closer_variant": "إغلاق",
        "transition_variants": ["أول", "ثان", "ثالث"],
        "editorial_intent": {
            "editorial_thesis": "الدافع يتغير حين نفهم سبب تعثرنا بدل لوم أنفسنا",
            "viewer_starting_belief": "يظن المشاهد أن فقدان الدافع يعني أن الإرادة ضعيفة",
            "hidden_assumption": "يفترض أن الحماس يجب أن يسبق كل خطوة عملية",
            "editorial_turn": "يرى أن الحركة الصغيرة قد تسبق الحماس وتعيد بناءه",
            "stakes": "استمرار هذا الفهم يحدد هل يعود للمحاولة أم ينسحب مبكرًا",
            "viewer_promise": "سيفهم لماذا تتعطل البداية وكيف يصنع خطوة قابلة للتكرار",
            "evidence_boundaries": [
                "لا نحول الفكرة إلى تشخيص طبي ولا ندعي نتيجة مضمونة"
            ],
            "earned_payoff": "يخرج بنموذج أبسط يربط الفعل الصغير باستعادة الزخم تدريجيًا",
        },
    }


class PlanningSplitRetryRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "checkpoint.json"
        self.old_json_text = staged.json_text
        self.old_schema_tuple = contract._schema_tuple
        self.old_validate_response = contract.validate_response
        self.had_split_schema_marker = hasattr(
            contract, "_ISCO_OUTLINE_SPLIT_SCHEMA_ADAPTER_V2"
        )
        self.old_split_schema_marker = getattr(
            contract, "_ISCO_OUTLINE_SPLIT_SCHEMA_ADAPTER_V2", None
        )
        self.cache_patch = mock.patch.object(router, "CACHE_PATH", self.cache)
        self.sleep_patch = mock.patch.object(contract.time, "sleep")
        self.router_sleep_patch = mock.patch.object(router.time, "sleep")
        self.cache_patch.start()
        self.sleep_patch.start()
        self.router_sleep_patch.start()
        staged.json_text = lambda *_args, **_kwargs: {}
        router._USED_PROVIDERS.clear()
        router._TELEMETRY.clear()
        checkpoint_guard.install_checkpoint_namespace_guard()
        contract.install_planning_contract_router()

        # This regression invokes a split StageSpec directly instead of booting the
        # whole production lifecycle. Production installs this adapter last through
        # install_planning_outline_split_contract(); install the same owner here so the
        # retry test exercises the canonical Core/Sections OpenRouter schema boundary
        # rather than an impossible partially-installed runtime.
        contract._ISCO_OUTLINE_SPLIT_SCHEMA_ADAPTER_V2 = False
        split._install_schema_and_validation_adapters()

    def tearDown(self) -> None:
        staged.json_text = self.old_json_text
        contract._schema_tuple = self.old_schema_tuple
        contract.validate_response = self.old_validate_response
        if self.had_split_schema_marker:
            contract._ISCO_OUTLINE_SPLIT_SCHEMA_ADAPTER_V2 = self.old_split_schema_marker
        elif hasattr(contract, "_ISCO_OUTLINE_SPLIT_SCHEMA_ADAPTER_V2"):
            delattr(contract, "_ISCO_OUTLINE_SPLIT_SCHEMA_ADAPTER_V2")
        self.router_sleep_patch.stop()
        self.sleep_patch.stop()
        self.cache_patch.stop()
        self.tmp.cleanup()

    def test_mixed_failures_try_all_families_before_retrying_only_transient_gemini(self) -> None:
        payload = _core_payload()
        calls = {"gemini": 0, "groq": 0, "openrouter": 0}
        order: list[str] = []

        def gemini(*_args, **_kwargs):
            calls["gemini"] += 1
            order.append("gemini")
            if calls["gemini"] == 1:
                raise TimeoutError("client-side timeout")
            return payload

        def groq(_prompt):
            calls["groq"] += 1
            order.append("groq")
            raise RuntimeError("GROQ_TPM_WINDOW_BUSY_PRECHECK tpm_capacity")

        def openrouter(*_args, **_kwargs):
            calls["openrouter"] += 1
            order.append("openrouter")
            raise RuntimeError(
                "OPENROUTER_UNAVAILABLE_THIS_RUN reason=preflight_blocked: "
                "key spend capacity exhausted"
            )

        spec = split.outline_core_stage_spec_for_format("film")
        with mock.patch.object(router, "gemini_json_text", side_effect=gemini), \
                mock.patch.object(router, "_groq_call", side_effect=groq), \
                mock.patch.object(router, "_openrouter_call_with_repair", side_effect=openrouter), \
                contract.request_stage_scope(spec):
            result = staged.json_text("request-key", "opaque")

        self.assertEqual(result, payload)
        self.assertEqual(calls, {"gemini": 2, "groq": 1, "openrouter": 1})
        self.assertEqual(order, ["gemini", "groq", "openrouter", "gemini"])

    def test_capacity_failure_is_not_retried_for_same_provider(self) -> None:
        calls = {"groq": 0}
        base = split.outline_core_stage_spec_for_format("film")
        policy = contract.ProviderPolicy(
            providers=("groq",),
            max_attempts_per_provider=1,
            max_total_attempts=2,
            completion_tokens=base.provider_policy.completion_tokens,
            max_prompt_utf8_bytes=base.provider_policy.max_prompt_utf8_bytes,
            second_pass_after_full_exhaustion=True,
        )
        spec = contract.PlanningStageSpec(
            stage_id=base.stage_id,
            contract_id=base.contract_id,
            output_schema=base.output_schema,
            semantic_rules=base.semantic_rules,
            provider_policy=policy,
            cache_policy=base.cache_policy,
        )

        def groq(_prompt):
            calls["groq"] += 1
            raise RuntimeError("GROQ_TPM_WINDOW_BUSY_PRECHECK tpm_capacity")

        with mock.patch.object(router, "_groq_call", side_effect=groq), \
                contract.request_stage_scope(spec):
            with self.assertRaises(contract.PlanningStageError) as captured:
                staged.json_text("request-key", "opaque")

        self.assertEqual(captured.exception.code, contract.PlanningErrorCode.CAPACITY)
        self.assertEqual(calls["groq"], 1)


if __name__ == "__main__":
    unittest.main()
