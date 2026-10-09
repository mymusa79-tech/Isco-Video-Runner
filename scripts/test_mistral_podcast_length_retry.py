import unittest

from clean_v2.providers import (
    ProviderAdapter,
    ProviderRouter,
    _mistral_podcast_length_validator_retry_prompt,
)


def _router(calls):
    def fake(prompt, max_tokens, stage):
        calls.append(prompt)
        return {"n": len(calls)}
    return ProviderRouter((ProviderAdapter("mistral", fake, accepts_stage=True),))


class MistralPodcastLengthRetryTests(unittest.TestCase):
    def test_bounded_shortfall_gets_one_same_provider_retry(self):
        calls = []
        router = _router(calls)

        def validator(candidate):
            if candidate["n"] == 1:
                raise RuntimeError(
                    "podcast_estimated_duration_too_short "
                    "estimated_seconds=342.9 minimum=480.0 words=600 minimum_words=840"
                )
            return {"ok": True}

        out = router.route(
            stage="script",
            prompt="BASE PODCAST DEPTH REPAIR",
            max_tokens=18000,
            validator=validator,
        )

        self.assertEqual(out, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertIn("MISTRAL_PODCAST_LENGTH_VALIDATOR_RETRY", calls[1])
        self.assertIn("at least 840 spoken words", calls[1])
        self.assertIn("Do NOT pad", calls[1])
        self.assertTrue(
            any(
                event["reason"] == "mistral_podcast_length_validator_retry"
                for event in router.events
            )
        )

    def test_retry_is_bounded_to_one(self):
        calls = []
        router = _router(calls)

        with self.assertRaises(RuntimeError):
            router.route(
                stage="script",
                prompt="BASE PODCAST DEPTH REPAIR",
                max_tokens=18000,
                validator=lambda _candidate: (_ for _ in ()).throw(
                    RuntimeError(
                        "podcast_estimated_duration_too_short "
                        "estimated_seconds=342.9 minimum=480.0 words=600 minimum_words=840"
                    )
                ),
            )

        self.assertEqual(len(calls), 2)

    def test_severely_short_or_unrelated_rejection_does_not_retry(self):
        self.assertIsNone(
            _mistral_podcast_length_validator_retry_prompt(
                "BASE",
                RuntimeError(
                    "podcast_estimated_duration_too_short "
                    "estimated_seconds=228.6 minimum=480.0 words=400 minimum_words=840"
                ),
            )
        )
        self.assertIsNone(
            _mistral_podcast_length_validator_retry_prompt(
                "BASE",
                RuntimeError(
                    "podcast_listener_proxy_question_too_long words=23 maximum=20"
                ),
            )
        )
        self.assertIsNone(
            _mistral_podcast_length_validator_retry_prompt(
                "BASE",
                ValueError(
                    "podcast_estimated_duration_too_short "
                    "estimated_seconds=342.9 minimum=480.0 words=600 minimum_words=840"
                ),
            )
        )


if __name__ == "__main__":
    unittest.main()
