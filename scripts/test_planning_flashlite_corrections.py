import unittest

from clean_v2.providers import ProviderAdapter, ProviderRouter


class PlanningFlashLiteCorrections(unittest.TestCase):
    def test_flash_lite_planning_gets_named_rule_corrections_up_to_two(self):
        calls = []

        def fake(prompt, max_tokens, stage):
            calls.append(prompt)
            return {"n": len(calls)}

        router = ProviderRouter((ProviderAdapter("gemini_flash_lite", fake, accepts_stage=True),))
        errors = {1: "visual_story Short visual family exceeds two beats: phone",
                  2: "visual_story beat b5 stock_query_en must stay English"}

        def validator(c):
            if c["n"] in errors:
                raise ValueError(errors[c["n"]])
            return {"ok": True}

        out = router.route(stage="planning", prompt="BASE", max_tokens=100, validator=validator)
        self.assertEqual(out, {"ok": True})
        self.assertEqual(len(calls), 3)
        self.assertIn("exceeds two beats", calls[1])
        self.assertTrue(any(e["reason"] == "mistral_planning_validator_retry" for e in router.events))


if __name__ == "__main__":
    unittest.main()
