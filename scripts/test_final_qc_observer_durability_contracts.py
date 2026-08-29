from __future__ import annotations

import hashlib
import unittest
from pathlib import Path


class FinalQcObserverDurabilityContractTests(unittest.TestCase):
    @staticmethod
    def _git_blob_sha(path: str) -> str:
        payload = Path(path).read_bytes()
        return hashlib.sha1(f"blob {len(payload)}\0".encode("ascii") + payload).hexdigest()

    def test_lower_authority_layers_remain_byte_identical(self) -> None:
        self.assertEqual(
            self._git_blob_sha("scripts/release_transaction.py"),
            "4cce337b12d01de9b3d4d2cce71322f1d68a7640",
        )
        self.assertEqual(
            self._git_blob_sha("scripts/render_durable_cache.py"),
            "abc92b472373cada7b92a7a53007ae943de98b27",
        )
        self.assertEqual(
            self._git_blob_sha("scripts/tts_durable_cache_semantics.py"),
            "518ce3cdd0a86399de24674ad458a8d6c84f9c12",
        )
        self.assertEqual(
            self._git_blob_sha("scripts/media_durable_cache.py"),
            "4749851eddc2de8550e2232893a2b588b7359846",
        )

    def test_original_qc_and_observers_remain_byte_identical(self) -> None:
        self.assertEqual(
            self._git_blob_sha("scripts/final_master_qc.py"),
            "e3412fc5710618eb9d7529710d8dbbc539e9fa91",
        )
        self.assertEqual(
            self._git_blob_sha("scripts/groq_audio_audit.py"),
            "0d4b927139de6ee1cc761045d16b1b87e0cdd55e",
        )
        self.assertEqual(
            self._git_blob_sha("scripts/voice_identity_observer.py"),
            "dd27614a31b6e7965ff9b3656fb0d6eecd753159",
        )
        self.assertEqual(
            self._git_blob_sha("scripts/analytics_observer_status.py"),
            "2d83e40e3d29d8e2ff9d86c0e689f0be3c3dcf3d",
        )

    def test_final_qc_cache_is_pass_only_and_exact_runtime_bound(self) -> None:
        source = Path("scripts/final_qc_observer_durability.py").read_text(encoding="utf-8")
        self.assertIn('document.get("status") == "pass"', source)
        self.assertIn('document.get("full_decode_ok") is True', source)
        self.assertIn('document.get("full_decode_timed_out") is False', source)
        self.assertIn('document.get("final_media_mutated") is False', source)
        self.assertIn('"implementation_sha256": _module_sha(final_master_qc)', source)
        self.assertIn('"media_tools": tools', source)
        self.assertIn('"final": _regular_file_binding(root / "final.mp4")', source)
        self.assertIn('"timeline": _regular_file_binding(root / "visual-timeline.json"', source)

    def test_groq_cache_is_retry_scoped_and_never_caches_errors(self) -> None:
        source = Path("scripts/final_qc_observer_durability.py").read_text(encoding="utf-8")
        self.assertIn("_contract_identity(require_run_id=True)", source)
        self.assertIn('document.get("decision") in {"pass", "review"}', source)
        self.assertIn('governor.get("status") == "ok"', source)
        self.assertNotIn('"audit_skipped"}', source)

    def test_analytics_is_intentionally_live(self) -> None:
        runtime = Path("scripts/runtime_closure.py").read_text(encoding="utf-8")
        durability = Path("scripts/final_qc_observer_durability.py").read_text(encoding="utf-8")
        self.assertNotIn("observe_post_acceptance_analytics", durability)
        self.assertIn("analytics remains intentionally live", runtime)

    def test_shared_transport_addition_does_not_change_existing_semantic_fingerprints(self) -> None:
        source = Path("scripts/durable_stage_cache.py").read_text(encoding="utf-8")
        self.assertIn("final_observer_valid", source)
        self.assertIn("prepare_final_observers", source)
        self.assertNotIn("CACHE_NAMESPACE", source)


if __name__ == "__main__":
    unittest.main()
