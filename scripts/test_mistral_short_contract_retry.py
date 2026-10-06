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

    def test_retry_is_bounded_to_one(self):
        calls = []
        router = _router(calls)
        with self.assertRaises(Exception):
            router.route(stage="script", prompt="BASE", max_tokens=100,
                         validator=lambda c: (_ for _ in ()).throw(
                             ShortFormatError("short_s3_payoff_contains_forbidden_action_family")))
        self.assertEqual(len(calls), 2)

    def test_other_codes_and_error_types_not_retried(self):
        for exc in (ShortFormatError("short_s3_forbids_joined_second_action"), ValueError("x")):
            self.assertIsNone(_mistral_short_contract_validator_retry_prompt("p", exc, "script"))


if __name__ == "__main__":
    unittest.main()
