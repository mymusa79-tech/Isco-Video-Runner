from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PlanningEndToEndCompositionTests(unittest.TestCase):
    def test_preflight_and_live_split_requests_are_exact_after_all_planning_installers(self) -> None:
        """Certify the merged Planning stack as one fresh-process request pipeline.

        The certified production Engine still uses the legacy two-call transport, while
        the candidate Engine adds a dynamic Global Skeleton to the same two-call seam.
        Legacy therefore remains byte-exact with preflight. Candidate runtime is checked
        against its actual post-installer request shapes and the same 8K Groq admission
        math; its Sections prompt cannot be precomputed byte-for-byte before Core returns
        the immutable Skeleton.
        """
        probe = textwrap.dedent(
            """
            import importlib.util
            import json
            import os
            from pathlib import Path

            import isco_video_agent.resilient_planner as staged
            from scripts import planning_envelope_preflight as preflight
            from scripts import planning_outline_split_contract as split
            from scripts import planning_stage_contract as stage
            from scripts import producer_quality_contract as producer
            from scripts import provider_capacity_hardening as capacity
            from scripts import task_level_planner_router as router
            from scripts.planning_runtime_contract import (
                install_entrypoint_planning_contracts,
                install_post_runtime_planning_contracts,
                install_runtime_planning_contracts,
            )

            adaptive_available = (
                importlib.util.find_spec("isco_video_agent.adaptive_outline_contract")
                is not None
            )
            if adaptive_available:
                from isco_video_agent import adaptive_outline_contract as engine_adaptive
                from scripts import planning_outline_adaptive_sharding as adaptive

            topic = "كيف تستعيد تركيزك بهدوء؟"
            research = {
                "approved_research_pack": [{"claim": "approved"}],
                "content_boundaries": ["stay within approved evidence"],
            }
            revision = producer.merge_producer_revision_note("", research, "film")
            policy = {}
            avoid = {}
            learning = {}
            premise = preflight._bounded_preflight_locked_premise()
            skeleton = [
                {
                    "id": f"s{index}",
                    "purpose": f"غرض القسم {index}",
                    "arc_position": index,
                }
                for index in range(1, 9)
            ]
            briefs = [
                {
                    "id": item["id"],
                    "purpose": item["purpose"],
                    "visual_query": "quiet room",
                    "on_screen_text": "نص",
                    "emotion": "calm",
                    "expected_seconds": 30,
                }
                for item in skeleton
            ]

            preflight.load_editorial_policy = lambda: policy
            preflight.novelty_context = lambda: avoid
            preflight.learning_context = lambda _fmt: learning
            preflight._bounded_preflight_locked_premise = lambda: premise
            preflight_prompts = []

            def capacity_probe(prompt, *, reserved_completion_tokens, contract_name):
                preflight_prompts.append(prompt)
                return {
                    "contract": contract_name,
                    "reserved_completion_tokens": reserved_completion_tokens,
                    "estimated_request_tokens": 100,
                    "provider_tpm_limit": 8000,
                }

            preflight.groq_capacity_estimate = capacity_probe
            preflight._split_outline_envelopes(
                brief={"approved_topic": topic},
                fmt="film",
                research=research,
            )
            assert len(preflight_prompts) == 2

            def engine_outline(api_key, **kwargs):
                core_prompt = staged.build_outline_structure_prompt(
                    topic=kwargs["topic"],
                    fmt=kwargs["fmt"],
                    policy_json=kwargs["policy_json"],
                    research_json=kwargs["research_json"],
                    avoid_json=kwargs["avoid_json"],
                    learning_json=kwargs["learning_json"],
                    revision_note=kwargs["revision_note"],
                )
                core = staged.json_text(api_key, core_prompt, model=kwargs["model"])
                sections_prompt = staged.build_outline_sections_prompt(
                    topic=kwargs["topic"],
                    fmt=kwargs["fmt"],
                    policy_json=kwargs["policy_json"],
                    research_json=kwargs["research_json"],
                    avoid_json=kwargs["avoid_json"],
                    revision_note=kwargs["revision_note"],
                    narrative_format=str(premise["narrative_format"]),
                    editorial_intent=dict(premise["editorial_intent"]),
                    pillar=str(premise["pillar"]),
                    hook=str(premise["hook"]),
                    closing_payoff=str(premise["closing_payoff"]),
                )
                sections = staged.json_text(api_key, sections_prompt, model=kwargs["model"])
                if adaptive_available:
                    merged = dict(core)
                    merged.update(sections)
                    return merged
                return {"core": core, "sections": sections}

            staged._outline = engine_outline
            router.CACHE_PATH = Path(os.environ["ISCO_TEST_TMP"]) / "planning-checkpoint.json"
            install_entrypoint_planning_contracts()
            install_runtime_planning_contracts()
            install_post_runtime_planning_contracts()

            runtime_prompts = []
            stages = []
            runtime_contracts = []

            def fake_gemini(api_key, prompt, model="gemini-2.5-flash", **kwargs):
                contract = stage._ACTIVE_REQUEST_CONTRACT.get()
                assert contract is not None
                stages.append(contract.stage_id)
                runtime_prompts.append(prompt)
                runtime_contracts.append(contract)
                if not adaptive_available:
                    return {}
                if contract.stage_id == "planning.editorial_outline_core":
                    payload = dict(premise)
                    payload[engine_adaptive.GLOBAL_SECTION_SKELETON_FIELD] = skeleton
                    return payload
                return {"section_briefs": briefs}

            router.gemini_json_text = fake_gemini
            stage.validate_response = lambda contract, data: data
            split._validate_canonical_outline = lambda data, contract, expected: data

            result = staged._outline(
                "request-key",
                topic=topic,
                fmt="film",
                model="gemini-2.5-flash",
                policy_json=json.dumps(policy, ensure_ascii=False),
                research_json=json.dumps(research, ensure_ascii=False),
                avoid_json=json.dumps(avoid, ensure_ascii=False),
                learning_json=json.dumps(learning, ensure_ascii=False),
                revision_note=revision,
            )

            assert stages == [
                "planning.editorial_outline_core",
                "planning.editorial_outline_sections",
            ]

            if not adaptive_available:
                assert runtime_prompts == preflight_prompts
                for spec in (
                    split.outline_core_stage_spec_for_format("film"),
                    split.outline_sections_stage_spec_for_format("film"),
                ):
                    assert spec.provider_policy.max_attempts_per_provider == 1
                    assert spec.provider_policy.max_total_attempts == 6
                    assert spec.provider_policy.completion_tokens == 2400
                    assert spec.provider_policy.completion_tokens_for("gemini") == 4800
            else:
                assert getattr(staged.json_text, adaptive._ADAPTIVE_JSON_MARKER, False)
                assert engine_adaptive.GLOBAL_SECTION_SKELETON_MARKER in runtime_prompts[0]
                assert engine_adaptive.SECTION_SHARD_MARKER in runtime_prompts[1]
                assert "GLOBAL_SECTION_SKELETON" in runtime_prompts[1]
                assert [item["id"] for item in result["section_briefs"]] == [
                    f"s{i}" for i in range(1, 9)
                ]
                assert runtime_contracts[0].provider_policy.max_total_attempts == 6
                assert runtime_contracts[1].provider_policy.max_total_attempts == 6
                assert runtime_contracts[0].provider_policy.completion_tokens == 2400
                assert runtime_contracts[1].provider_policy.completion_tokens == 1800
                for prompt, contract in zip(runtime_prompts, runtime_contracts):
                    estimate = capacity.groq_capacity_estimate(
                        prompt,
                        reserved_completion_tokens=contract.provider_policy.completion_tokens,
                        contract_name=str(contract.semantic_rules["transport_profile"]),
                    )
                    assert int(estimate["estimated_request_tokens"]) <= 8000, estimate
            """
        )

        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["ISCO_TEST_TMP"] = tmp
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            for name in (
                "ISCO_CANONICAL_RUNTIME",
                "GITHUB_ACTIONS",
                "GITHUB_EVENT_NAME",
                "GITHUB_WORKFLOW_REF",
            ):
                env.pop(name, None)
            completed = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=90,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stdout)


if __name__ == "__main__":
    unittest.main()
