from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from isco_video_agent.visual_selection import VisualCandidateCache, review_candidates

import scripts.security_v1_live_binding as security_binding


class Run108StockCandidateIsolationTests(unittest.TestCase):
    def _audit_adapter(self, wrapped):
        def audit_fn(*, provider: str, candidate: dict, narration_context: str, intended_visual: str) -> dict:
            del provider, narration_context, intended_visual
            return wrapped("key", Path(f"candidate-{candidate['id']}.mp4"))

        return audit_fn

    def test_first_security_rejected_candidate_does_not_kill_bounded_selection(self) -> None:
        cloud_calls: list[str] = []

        def cloud_model(*_args, **_kwargs):
            cloud_calls.append("vision")
            return {"status": "pass", "relevance": 9.0, "visual_quality": 9.0}

        wrapped = security_binding._wrap_vision_audit(
            cloud_model,
            isolate_stock_candidate_failures=True,
        )
        side_effects = [
            RuntimeError("multimodal_injection_firewall_block:qr_code_detected"),
            None,
        ]
        with patch.object(security_binding, "_scan_media_before_vision", side_effect=side_effects):
            result = review_candidates(
                [("pexels", {"id": 101}), ("pexels", {"id": 102})],
                narration_context="narration",
                intended_visual="quiet street",
                audit_fn=self._audit_adapter(wrapped),
                cache=VisualCandidateCache(excluded_assets={}),
                max_candidates=2,
            )

        self.assertEqual(result.status, "selected")
        self.assertIsNotNone(result.chosen)
        self.assertEqual(result.chosen.candidate["id"], 102)
        self.assertEqual(len(result.reviewed), 2)
        self.assertEqual(result.reviewed[0].audit["status"], "block")
        self.assertEqual(result.reviewed[0].audit["local_media_rejection"], "qr_code_detected")
        self.assertEqual(result.reviewed[1].audit["status"], "pass")
        self.assertEqual(cloud_calls, ["vision"])

    def test_all_security_rejected_candidates_fail_closed_after_bounded_budget(self) -> None:
        cloud_calls: list[str] = []

        def cloud_model(*_args, **_kwargs):
            cloud_calls.append("vision")
            return {"status": "pass"}

        wrapped = security_binding._wrap_vision_audit(
            cloud_model,
            isolate_stock_candidate_failures=True,
        )
        side_effects = [
            RuntimeError("multimodal_injection_firewall_block:qr_code_detected"),
            RuntimeError("multimodal_injection_firewall_block:barcode_detected"),
        ]
        with patch.object(security_binding, "_scan_media_before_vision", side_effect=side_effects):
            result = review_candidates(
                [("pexels", {"id": 201}), ("pixabay", {"id": 202})],
                narration_context="narration",
                intended_visual="office desk",
                audit_fn=self._audit_adapter(wrapped),
                cache=VisualCandidateCache(excluded_assets={}),
                max_candidates=2,
            )

        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.chosen)
        self.assertEqual(len(result.reviewed), 2)
        self.assertEqual(
            [review.audit["local_media_rejection"] for review in result.reviewed],
            ["qr_code_detected", "barcode_detected"],
        )
        self.assertEqual(cloud_calls, [])

    def test_unknown_future_security_code_still_aborts_selection(self) -> None:
        wrapped = security_binding._wrap_vision_audit(
            lambda *_a, **_k: {"status": "pass"},
            isolate_stock_candidate_failures=True,
        )
        with patch.object(
            security_binding,
            "_scan_media_before_vision",
            side_effect=RuntimeError(
                "multimodal_injection_firewall_block:future_security_code"
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "future_security_code"):
                review_candidates(
                    [("pexels", {"id": 301}), ("pexels", {"id": 302})],
                    narration_context="narration",
                    intended_visual="city skyline",
                    audit_fn=self._audit_adapter(wrapped),
                    cache=VisualCandidateCache(excluded_assets={}),
                    max_candidates=2,
                )


if __name__ == "__main__":
    unittest.main()
