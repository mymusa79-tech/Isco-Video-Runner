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

        This catches the failure class where individually-green prompt/contract/provider
        branches compose into a different final request at runtime than preflight sized.
        """
        probe = textwrap.dedent(
            """
            import json
            import os
            from pathlib import Path

            import isco_video_agent.resilient_planner as staged
            from scripts import planning_envelope_preflight as preflight
            from scripts import planning_outline_split_contract as split
            from scripts import planning_stage_contract as stage
            from scripts import producer_quality_contract as producer
            from scripts import task_level_planner_router as router
            from scripts.planning_runtime_contract import (
                install_entrypoint_planning_contracts,
                install_post_runtime_planning_contracts,
                install_runtime_planning_contracts,
            )

            topic = "كيف تستعيد تركيزك بهدوء؟"
            research = {
                "approved_research_pack": [{"claim": "approved"}],
                "content_boundaries": ["stay within approved evidence"],
            }
            revision = producer.merge_producer_revision_note("", research)
            policy = {}
            avoid = {}
            learning = {}
            premise = preflight._bounded_preflight_locked_premise()

            # Give preflight and the fake pinned-Engine topology the exact same host
            # inputs. Capture the final request bytes handed to capacity admission.
            preflight.load_editorial_policy = lambda: policy
            preflight.novelty_context = lambda: avoid
            preflight.learning_context = lambda _fmt: learning
            preflight._bounded_preflight_locked_premise = lambda: premise
            preflight_prompts = []

            def capacity(prompt, *, reserved_completion_tokens, contract_name):
                preflight_prompts.append(prompt)
                return {
                    "contract": contract_name,
                    "reserved_completion_tokens": reserved_completion_tokens,
                    "estimated_request_tokens": 100,
                    "provider_tpm_limit": 8000,
                }

            preflight.groq_capacity_estimate = capacity
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
                return {"core": core, "sections": sections}

            # Match the exact merged installer order, but keep provider I/O mocked.
            staged._outline = engine_outline
            router.CACHE_PATH = Path(os.environ["ISCO_TEST_TMP"]) / "planning-checkpoint.json"
            install_entrypoint_planning_contracts()
            install_runtime_planning_contracts()
            install_post_runtime_planning_contracts()

            runtime_prompts = []
            stages = []

            def fake_gemini(api_key, prompt, model="gemini-2.5-flash", **kwargs):
                contract = stage._ACTIVE_REQUEST_CONTRACT.get()
                assert contract is not None
                stages.append(contract.stage_id)
                runtime_prompts.append(prompt)
                return {}

            router.gemini_json_text = fake_gemini
            stage.validate_response = lambda contract, data: data
            split._validate_canonical_outline = lambda data, contract, expected: data

            staged._outline(
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
            assert runtime_prompts == preflight_prompts, (
                len(runtime_prompts[0]), len(preflight_prompts[0]),
                len(runtime_prompts[1]), len(preflight_prompts[1])
            )
            for spec in (
                split.outline_core_stage_spec_for_format("film"),
                split.outline_sections_stage_spec_for_format("film"),
            ):
                assert spec.provider_policy.max_attempts_per_provider == 1
                assert spec.provider_policy.max_total_attempts == 6
                assert spec.provider_policy.completion_tokens == 2400
                assert spec.provider_policy.completion_tokens_for("gemini") == 4800
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
