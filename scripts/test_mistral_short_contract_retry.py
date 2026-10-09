import unittest

from clean_v2.providers import (
    ProviderAdapter,
    ProviderRouter,
    _mistral_short_contract_validator_retry_prompt,
)
from clean_v2.short_format import ShortFormatError


def _router(calls, stage_ok_on=2):
    def fake(prompt, max_tokens, stage):
        calls.append(prompt)
        return {"n": len(calls)}
    return ProviderRouter((ProviderAdapter("mistral", fake, accepts_stage=True),))


class MistralShortContractRetryTests(unittest.TestCase):
    def _run(self, stage, err):
        calls = []
        router = _router(calls)

        def validator(c):
            if c["n"] == 1:
                raise ShortFormatError(err)
            return {"ok": True}

        out = router.route(stage=stage, prompt="BASE", max_tokens=100, validator=validator)
        return calls, out, router.events

    def test_payoff_action_family_retried_once_in_script_and_patch(self):
        for stage in ("script", "script_patch"):
            calls, out, events = self._run(stage, "short_s3_payoff_contains_forbidden_action_family")
            self.assertEqual(len(calls), 2, stage)
            self.assertIn("MISTRAL_SHORT_S3_PAYOFF_VALIDATOR_RETRY", calls[1])
            self.assertIn("locked s3 action sentence", calls[1])
            self.assertEqual(out, {"ok": True})
            self.assertTrue(any(e["reason"] == "mistral_short_contract_validator_retry" for e in events))

    def test_hook_tension_retried_in_script_only(self):
        calls, _, _ = self._run("script", "short_hook_requires_immediate_concrete_tension")
        self.assertEqual(len(calls), 2)
        self.assertIn("MISTRAL_SHORT_HOOK_TENSION_VALIDATOR_RETRY", calls[1])
        self.assertIsNone(_mistral_short_contract_validator_retry_prompt(
            "p", ShortFormatError("short_hook_requires_immediate_concrete_tension"), "script_patch"))

    def test_same_rule_repeated_is_not_corrected_twice(self):
        calls = []
        router = _router(calls)
        with self.assertRaises(Exception):
            router.route(stage="script", prompt="BASE", max_tokens=100,
                         validator=lambda c: (_ for _ in ()).throw(
                             ShortFormatError("short_s3_payoff_contains_forbidden_action_family")))
        self.assertEqual(len(calls), 2)

    def test_unlisted_short_rule_gets_generic_correction_other_errors_do_not(self):
        generic = _mistral_short_contract_validator_retry_prompt(
            "p", ShortFormatError("short_s3_forbids_joined_second_action"), "script")
        self.assertIn("short_s3_forbids_joined_second_action", generic)
        self.assertIn("SHORT_CONTRACT_VALIDATOR_RETRY", generic)
        for exc in (ValueError("x"), RuntimeError("short_x")):
            self.assertIsNone(_mistral_short_contract_validator_retry_prompt("p", exc, "script"))

    def test_flash_lite_also_gets_corrections_and_a_changed_rule_gets_its_own(self):
        calls = []

        def fake(prompt, max_tokens, stage):
            calls.append(prompt)
            return {"n": len(calls)}

        router = ProviderRouter((ProviderAdapter("gemini_flash_lite", fake, accepts_stage=True),))
        errors = {1: "short_hook_requires_immediate_concrete_tension",
                  2: "short_s3_payoff_contains_forbidden_action_family"}

        def validator(c):
            if c["n"] in errors:
                raise ShortFormatError(errors[c["n"]])
            return {"ok": True}

        out = router.route(stage="script", prompt="BASE", max_tokens=100, validator=validator)
        self.assertEqual(out, {"ok": True})
        self.assertEqual(len(calls), 3)
        self.assertIn("MISTRAL_SHORT_HOOK_TENSION_VALIDATOR_RETRY", calls[1])
        self.assertIn("MISTRAL_SHORT_S3_PAYOFF_VALIDATOR_RETRY", calls[2])


if __name__ == "__main__":
    unittest.main()
