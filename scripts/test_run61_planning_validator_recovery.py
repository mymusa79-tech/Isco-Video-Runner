from __future__ import annotations

import json
import unittest
from unittest import mock

from clean_v2 import pipeline
from clean_v2.providers import (
    ProviderAdapter,
    ProviderRouter,
    _mistral_planning_validator_retry_prompt,
    _mistral_script_patch_validator_retry_prompt,
    _safe_mistral_planning_raw_diagnostic,
)
from clean_v2.visual_story import CHANNEL_VISUAL_IDENTITY, VisualWorldIdentityError


class Run61PlanningValidatorRecoveryTests(unittest.TestCase):
    def test_mistral_planning_gets_one_validator_guided_retry(self) -> None:
        calls: list[str] = []

        def fake_call(prompt: str, max_tokens: int, stage: str) -> dict:
            self.assertEqual(max_tokens, 3000)
            self.assertEqual(stage, "planning")
            calls.append(prompt)
            return {"attempt": len(calls)}

        def validator(candidate: dict) -> dict:
            if candidate["attempt"] == 1:
                raise ValueError(
                    "short visual story requires three authored s1 hook beats "
                    "plus one s2 and one s3 beat"
                )
            return {"status": "pass"}

        router = ProviderRouter(
            (
                ProviderAdapter(
                    "mistral",
                    fake_call,
                    stages=frozenset({"planning"}),
                    accepts_stage=True,
                ),
            )
        )
        result = router.route(
            stage="planning",
            prompt="APPROVED_BRIEF:\n{}\n\nBuild a simple production plan.",
            max_tokens=3000,
            validator=validator,
        )

        self.assertEqual(result, {"status": "pass"})
        self.assertEqual(len(calls), 2)
        self.assertIn("MISTRAL_PLANNING_VALIDATOR_RETRY", calls[1])
        self.assertIn("three authored s1 hook beats", calls[1])
        self.assertEqual(
            [event["result"] for event in router.events],
            ["retrying", "success"],
        )
        self.assertEqual(
            router.events[0]["reason"],
            "mistral_planning_validator_retry",
        )

    def test_run83_planning_retry_targets_post_hook_semantic_drop(self) -> None:
        prompt = _mistral_planning_validator_retry_prompt(
            "BASE",
            ValueError(
                "visual_story beat b4 post-hook semantic drop requires a stronger observable alternate"
            ),
        )
        self.assertIsNotNone(prompt)
        self.assertIn("For b4", prompt)
        self.assertIn("too generic", prompt)
        self.assertIn("stock_query_alt_en", prompt)
        self.assertIn("Do not return generic typing", prompt)

    def test_run84_planning_retry_targets_missing_sections(self) -> None:
        prompt = _mistral_planning_validator_retry_prompt(
            "BASE",
            ValueError("visual_story must cover every planned section: missing=s4,s5"),
        )
        self.assertIsNotNone(prompt)
        self.assertIn("s4,s5", prompt)
        self.assertIn("EVERY named missing section", prompt)
        self.assertIn("section_id exactly matches", prompt)

    def test_run85_script_patch_gets_one_semantic_coverage_retry(self) -> None:
        calls: list[str] = []

        def fake_call(prompt: str, max_tokens: int, stage: str) -> dict:
            self.assertEqual(stage, "script_patch")
            self.assertEqual(max_tokens, 1200)
            calls.append(prompt)
            return {"attempt": len(calls)}

        def validator(candidate: dict) -> dict:
            if candidate["attempt"] == 1:
                raise ValueError(
                    "semantic script patch did not change every explicitly flagged section: s1"
                )
            return {"status": "pass"}

        router = ProviderRouter(
            (
                ProviderAdapter(
                    "mistral",
                    fake_call,
                    stages=frozenset({"script_patch"}),
                    accepts_stage=True,
                ),
            )
        )
        result = router.route(
            stage="script_patch",
            prompt="CURRENT_SCRIPT:\n{}\nREQUIRED_SEMANTIC_CHANGE_SECTION_IDS:[\"s1\"]",
            max_tokens=1200,
            validator=validator,
        )
        self.assertEqual(result, {"status": "pass"})
        self.assertEqual(len(calls), 2)
        self.assertIn("MISTRAL_SCRIPT_PATCH_VALIDATOR_RETRY", calls[1])
        self.assertIn("Missing required section ids: s1", calls[1])
        self.assertEqual(
            [event["result"] for event in router.events],
            ["retrying", "success"],
        )
        self.assertEqual(
            router.events[0]["reason"],
            "mistral_script_patch_validator_retry",
        )

    def test_script_patch_retry_is_only_for_missing_required_semantic_coverage(self) -> None:
        self.assertIsNone(
            _mistral_script_patch_validator_retry_prompt(
                "BASE",
                ValueError("script patch changed locked prayer sentence"),
            )
        )

    def test_visual_identity_is_host_normalized_on_first_rejection(self) -> None:
        router = type("Router", (), {"events": []})()
        state = {"identity_rejections": 0}
        candidate = {
            "visual_story": {"visual_world": "bright generic lifestyle"},
        }
        calls: list[dict] = []

        def fake_validate(value, _brief, *, enforce_visual_identity):
            self.assertTrue(enforce_visual_identity)
            calls.append(value)
            if len(calls) == 1:
                raise VisualWorldIdentityError("missing navy and gold")
            return value

        with mock.patch.object(
            pipeline,
            "_validate_plan_for_brief",
            side_effect=fake_validate,
        ):
            result = pipeline._validate_plan_with_visual_world_recovery(
                candidate,
                {"format": "film"},
                router=router,
                state=state,
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual(
            result["visual_story"]["visual_world"],
            CHANNEL_VISUAL_IDENTITY,
        )
        self.assertEqual(state["identity_rejections"], 1)
        self.assertEqual(router.events[-1]["provider"], "host")
        self.assertEqual(
            router.events[-1]["reason"],
            "visual_world_identity_fallback",
        )

    def test_retry_is_not_offered_for_unrelated_runtime_failure(self) -> None:
        self.assertIsNone(
            _mistral_planning_validator_retry_prompt(
                "prompt",
                RuntimeError("provider transport failed"),
            )
        )

    def test_planning_diagnostic_logs_shape_not_authored_text(self) -> None:
        raw = json.dumps(
            {
                "title": "SECRET_TITLE",
                "sections": [
                    {
                        "id": "s1",
                        "heading": "SECRET_HEADING",
                        "purpose": "SECRET_PURPOSE",
                        "visual_query_en": "SECRET_QUERY",
                    }
                ],
                "visual_story": {
                    "visual_world": "SECRET_WORLD",
                    "beats": [
                        {
                            "id": "b1",
                            "section_id": "s1",
                            "viewer_intent": "SECRET_INTENT",
                            "stock_query_en": "SECRET_STOCK_QUERY",
                        }
                    ],
                },
            },
            ensure_ascii=False,
        )
        diagnostic = _safe_mistral_planning_raw_diagnostic(
            raw,
            ValueError("short visual story requires five beats"),
        )
        encoded = json.dumps(diagnostic, ensure_ascii=False)

        self.assertIn("short visual story requires five beats", encoded)
        self.assertIn('"sections_count": 1', encoded)
        self.assertIn('"beats_count": 1', encoded)
        for secret in (
            "SECRET_TITLE",
            "SECRET_HEADING",
            "SECRET_PURPOSE",
            "SECRET_QUERY",
            "SECRET_WORLD",
            "SECRET_INTENT",
            "SECRET_STOCK_QUERY",
        ):
            self.assertNotIn(secret, encoded)


if __name__ == "__main__":
    unittest.main()
