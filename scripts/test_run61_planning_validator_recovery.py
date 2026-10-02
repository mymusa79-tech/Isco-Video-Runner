from __future__ import annotations

import json
import unittest

from clean_v2.providers import (
    ProviderAdapter,
    ProviderRouter,
    _mistral_planning_validator_retry_prompt,
    _safe_mistral_planning_raw_diagnostic,
)


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
