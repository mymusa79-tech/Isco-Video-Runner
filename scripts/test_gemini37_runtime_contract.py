from __future__ import annotations

import unittest

from isco_video_agent.providers import gemini as engine_gemini
from scripts import provider_preflight


CANONICAL_CONTENT_MODEL = "gemini-3.7-flash"
LEGACY_POLICY_ALIAS = "gemini-2.5-flash"


class Gemini37RuntimeContractTests(unittest.TestCase):
    def test_legacy_workflow_name_resolves_to_gemini37_at_network_boundary(self) -> None:
        self.assertEqual(
            provider_preflight._gemini_runtime_content_model(LEGACY_POLICY_ALIAS),
            CANONICAL_CONTENT_MODEL,
        )
        self.assertEqual(
            engine_gemini._content_model(LEGACY_POLICY_ALIAS),
            CANONICAL_CONTENT_MODEL,
        )

    def test_explicit_gemini37_remains_gemini37(self) -> None:
        self.assertEqual(
            provider_preflight._gemini_runtime_content_model(CANONICAL_CONTENT_MODEL),
            CANONICAL_CONTENT_MODEL,
        )
        self.assertEqual(
            engine_gemini._content_model(CANONICAL_CONTENT_MODEL),
            CANONICAL_CONTENT_MODEL,
        )

    def test_preflight_and_engine_resolvers_agree(self) -> None:
        for requested in (LEGACY_POLICY_ALIAS, CANONICAL_CONTENT_MODEL):
            self.assertEqual(
                provider_preflight._gemini_runtime_content_model(requested),
                engine_gemini._content_model(requested),
            )


if __name__ == "__main__":
    unittest.main()
