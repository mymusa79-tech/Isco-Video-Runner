import unittest
from unittest import mock

from clean_v2 import providers


class OpenRouter404DiagnosticTests(unittest.TestCase):
    def _call(self, exc):
        with mock.patch.object(providers, "_read_secret", return_value="k"), \
             mock.patch.object(providers, "_post_json", side_effect=exc):
            with self.assertRaises(providers.ProviderWireFailure) as ctx:
                providers._openrouter_call("p", 100)
        return ctx.exception

    def test_404_body_is_exposed_but_reason_code_unchanged(self):
        err = self._call(providers.ProviderWireFailure(
            "http_404", http_status=404,
            error_detail='{"error":{"message":"No endpoints found for google/gemma"}}'))
        self.assertEqual(err.reason_code, "http_404")
        self.assertEqual(err.http_status, 404)
        self.assertIn("No endpoints found", str(err))

    def test_classifier_keywords_are_neutralised(self):
        err = self._call(providers.ProviderWireFailure(
            "http_404", http_status=404,
            error_detail="network connection timeout 429 quota rate limit"))
        text = str(err).lower()
        for word in ("429", "quota", "rate limit", "timeout", "connection", "network"):
            self.assertNotIn(word, text)

    def test_other_statuses_untouched(self):
        orig = providers.ProviderWireFailure("http_500", http_status=500, error_detail="boom")
        err = self._call(orig)
        self.assertEqual(str(err), "http_500")

    def test_404_without_body_untouched(self):
        err = self._call(providers.ProviderWireFailure("http_404", http_status=404))
        self.assertEqual(str(err), "http_404")


if __name__ == "__main__":
    unittest.main()
