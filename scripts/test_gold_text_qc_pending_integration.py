from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts import gold_text_qc_pending_v1 as pending
from scripts import gold_vision_capacity_reserve_v1 as reserve


class GoldTextPendingIntegrationTests(unittest.TestCase):
    def test_shared_gold_installer_includes_text_pending_classifier(self) -> None:
        # The canonical production path and Gold-only resume both install this shared
        # owner. Keep the new text-outage classifier on that same seam so Long, Story,
        # Moment and derived Moment recovery cannot drift apart.
        with (
            patch.object(reserve, "_INSTALLED", False),
            patch.object(reserve, "install_vision_provider_failure_unification_v1") as vision,
            patch.object(reserve, "install_gold_text_qc_pending_v1") as text,
            patch.object(reserve, "_install_reserve_admission") as admission,
            patch.object(reserve, "_install_gold_groq_retry") as retry,
            patch.object(reserve, "_install_gold_scope") as scope,
        ):
            reserve.install_gold_vision_capacity_reserve_v1()
        vision.assert_called_once_with()
        text.assert_called_once_with()
        admission.assert_called_once_with()
        retry.assert_called_once_with()
        scope.assert_called_once_with()

    def test_text_exception_is_distinct_from_semantic_block_and_vision_taxonomy(self) -> None:
        exc = pending.GoldTextProviderMeshUnavailableError("technical mesh outage")
        self.assertIsInstance(exc, RuntimeError)
        self.assertEqual(type(exc).__name__, "GoldTextProviderMeshUnavailableError")
        self.assertNotEqual(type(exc).__name__, "VisionProviderMeshUnavailableError")


if __name__ == "__main__":
    unittest.main()
