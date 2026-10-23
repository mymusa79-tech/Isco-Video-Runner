import unittest

from clean_v2.media import TtsProviderError, VoiceInfrastructureError, _tts_quota_detail


BODY = (
    b'{"error":{"code":429,"message":"You exceeded your current quota, please check your plan.",'
    b'"details":[{"violations":[{"quotaMetric":"generativelanguage.googleapis.com/generate_requests_per_model_per_day",'
    b'"quotaId":"GenerateRequestsPerDayPerProjectPerModel","quotaValue":"100"}]}]}}'
)


class QuotaDetailTests(unittest.TestCase):
    def test_extracts_quota_ids_and_message(self):
        detail = _tts_quota_detail(BODY)
        self.assertIn("quotaValue=100", detail)
        self.assertIn("quotaId=GenerateRequestsPerDayPerProjectPerModel", detail)
        self.assertIn("message=You exceeded your current quota", detail)
        self.assertLessEqual(len(detail), 400)

    def test_empty_or_unrelated_body_gives_none(self):
        self.assertIsNone(_tts_quota_detail(b""))
        self.assertIsNone(_tts_quota_detail(b"<html>nope</html>"))

    def test_http_400_field_violation_and_key_redaction(self):
        body = (
            b'{"error":{"code":400,"message":"Invalid value at speech_config key=AIzaSyA1234567890abcdefghijklmnop",'
            b'"details":[{"fieldViolations":[{"field":"generation_config.speech_config","description":"speaker B has no turns"}]}]}}'
        )
        detail = _tts_quota_detail(body)
        self.assertIn("field=generation_config.speech_config", detail)
        self.assertIn("description=speaker B has no turns", detail)
        self.assertNotIn("AIza", detail)

    def test_attributes_carry_through(self):
        err = TtsProviderError("gemini_3_8_http_429", http_status=429, quota_detail="x")
        self.assertEqual(err.quota_detail, "x")
        infra = VoiceInfrastructureError(charon_attempts=1, charon_reason="r", quota_detail="x")
        self.assertEqual(infra.quota_detail, "x")


if __name__ == "__main__":
    unittest.main()
