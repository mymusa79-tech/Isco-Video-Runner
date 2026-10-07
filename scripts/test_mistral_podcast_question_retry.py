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
    def test_overlong_listener_question_gets_one_retry(self):
        calls = []
        router = _router(calls)

        def validator(candidate):
            if candidate["n"] == 1:
                raise RuntimeError(
                    "podcast_listener_proxy_question_too_long words=23 maximum=20"
                )
            return {"ok": True}

        result = router.route(
            stage="script",
            prompt="BASE PODCAST",
            max_tokens=100,
            validator=validator,
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertIn("MISTRAL_PODCAST_QUESTION_VALIDATOR_RETRY", calls[1])
        self.assertIn("18 words or fewer", calls[1])
        self.assertTrue(
            any(
                event["reason"] == "mistral_podcast_question_validator_retry"
                for event in router.events
            )
        )

    def test_retry_remains_bounded_to_one(self):
        calls = []
        router = _router(calls)

        with self.assertRaises(RuntimeError):
            router.route(
                stage="script",
                prompt="BASE PODCAST",
                max_tokens=100,
                validator=lambda candidate: (_ for _ in ()).throw(
                    RuntimeError(
                        "podcast_listener_proxy_question_too_long words=23 maximum=20"
                    )
                ),
            )

        self.assertEqual(len(calls), 2)

    def test_other_validator_errors_do_not_get_podcast_retry(self):
        self.assertIsNone(
            _mistral_podcast_question_validator_retry_prompt(
                "BASE",
                RuntimeError("podcast_estimated_duration_too_short words=337 minimum_words=420"),
            )
        )
        self.assertIsNone(
            _mistral_podcast_question_validator_retry_prompt(
                "BASE",
                RuntimeError("podcast_listener_proxy_question_too_long words=31 maximum=20"),
            )
        )
        self.assertIsNone(
            _mistral_podcast_question_validator_retry_prompt(
                "BASE",
                ValueError("podcast_listener_proxy_question_too_long words=23 maximum=20"),
            )
        )


if __name__ == "__main__":
    unittest.main()
