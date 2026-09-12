from __future__ import annotations

import unittest

from scripts import qc_pending_checkpoint_v1 as checkpoint


class GoldTextResumeContractTests(unittest.TestCase):
    def test_supported_pending_taxonomies_are_explicit_and_stage_specific(self) -> None:
        self.assertIn(
            ("GOLD_VISION_PENDING_PROVIDER_CAPACITY", "VisionProviderMeshUnavailableError"),
            checkpoint._SUPPORTED_FAILURES,
        )
        self.assertIn(
            ("GOLD_TEXT_PENDING_PROVIDER_CAPACITY", "GoldTextProviderMeshUnavailableError"),
            checkpoint._SUPPORTED_FAILURES,
        )
        self.assertEqual(len(checkpoint._SUPPORTED_FAILURES), 2)

    def test_generic_provider_wording_is_not_a_supported_resume_taxonomy(self) -> None:
        self.assertNotIn(
            ("GOLD_TEXT_PENDING_PROVIDER_CAPACITY", "RuntimeError"),
            checkpoint._SUPPORTED_FAILURES,
        )


if __name__ == "__main__":
    unittest.main()
