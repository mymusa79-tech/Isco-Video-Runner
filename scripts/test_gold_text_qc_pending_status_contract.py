from __future__ import annotations

import unittest

from scripts import qc_pending_checkpoint_v1 as checkpoint
from scripts.gold_text_qc_pending_v1 import GoldTextProviderMeshUnavailableError


class GoldTextPendingStatusContractTests(unittest.TestCase):
    def test_text_outage_maps_to_text_specific_status(self) -> None:
        self.assertEqual(
            checkpoint._gold_pending_failure(GoldTextProviderMeshUnavailableError("down")),
            ("GOLD_TEXT_PENDING_PROVIDER_CAPACITY", "GoldTextProviderMeshUnavailableError"),
        )

    def test_message_similarity_does_not_create_resume_authority(self) -> None:
        self.assertIsNone(
            checkpoint._gold_pending_failure(
                RuntimeError("Gold Text provider mesh unavailable after all providers failed")
            )
        )


if __name__ == "__main__":
    unittest.main()
