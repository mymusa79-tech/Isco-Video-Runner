import unittest

from clean_v2.providers import (
    ProviderAdapter,
    ProviderRouter,
    _mistral_podcast_question_validator_retry_prompt,
)


def _router(calls):
    def fake(prompt, max_tokens, stage):
        calls.append(prompt)
        return {"n": len(calls)}
    return ProviderRouter((ProviderAdapter("mistral", fake, accepts_stage=True),))


class MistralPodcastQuestionRetryTests(unittest.TestCase):
    def test_small_question_overrun_retried_once_without_relaxing_limit(self):
        calls = []
        router = _router(calls)

        def validator(candidate):
            if candidate["n"] == 1:
                raise RuntimeError(
                    "podcast_listener_proxy_question_too_long words=23 maximum=20"
                )
            return {"ok": True}

        out = router.route(
            stage="script",
            prompt="BASE PODCAST PROMPT",
            max_tokens=18000,
            validator=validator,
        )

        self.assertEqual(out, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertIn("MISTRAL_PODCAST_QUESTION_VALIDATOR_RETRY", calls[1])
        self.assertIn("hard rescue maximum 20", calls[1])
        self.assertIn("12-18 words", calls[1])
        self.assertTrue(
            any(
                event["reason"] == "mistral_podcast_question_validator_retry"
                for event in router.events
            )
        )

    def test_retry_is_bounded_to_one(self):
        calls = []
        router = _router(calls)

        with self.assertRaises(RuntimeError):
            router.route(
                stage="script",
                prompt="BASE PODCAST PROMPT",
                max_tokens=18000,
                validator=lambda _candidate: (_ for _ in ()).throw(
                    RuntimeError(
                        "podcast_listener_proxy_question_too_long words=23 maximum=20"
                    )
                ),
            )

        self.assertEqual(len(calls), 2)

    def test_large_or_unrelated_rejection_does_not_spend_retry(self):
        self.assertIsNone(
            _mistral_podcast_question_validator_retry_prompt(
                "BASE",
                RuntimeError(
                    "podcast_listener_proxy_question_too_long words=35 maximum=20"
                ),
            )
        )
        self.assertIsNone(
            _mistral_podcast_question_validator_retry_prompt(
                "BASE",
                RuntimeError("podcast_listener_proxy_questioner_dominates_episode"),
            )
        )
        self.assertIsNone(
            _mistral_podcast_question_validator_retry_prompt(
                "BASE",
                ValueError(
                    "podcast_listener_proxy_question_too_long words=23 maximum=20"
                ),
            )
        )


if __name__ == "__main__":
    unittest.main()
