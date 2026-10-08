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

    def test_attributes_carry_through(self):
        err = TtsProviderError("gemini_3_8_http_429", http_status=429, quota_detail="x")
        self.assertEqual(err.quota_detail, "x")
        infra = VoiceInfrastructureError(charon_attempts=1, charon_reason="r", quota_detail="x")
        self.assertEqual(infra.quota_detail, "x")


if __name__ == "__main__":
    unittest.main()
