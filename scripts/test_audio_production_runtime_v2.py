from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import runtime_closure
from scripts.audio_production_contract_v2 import AUDIT_FILENAME, CONTRACT_ID


class AudioProductionRuntimeV2Tests(unittest.TestCase):
    def _accepted(self, root: Path, *, decision: str = "pass") -> dict:
        final = root / "final.mp4"
        final.write_bytes(b"audio-production-runtime-v2" * 256)
        document = {
            "schema_version": 2,
            "contract_id": CONTRACT_ID,
            "decision": decision,
            "final_sha256": runtime_closure._sha256_file(final),
        }
        (root / AUDIT_FILENAME).write_text(json.dumps(document), encoding="utf-8")
        return document

    def test_post_gold_observer_skips_duplicate_provider_call_for_exact_accepted_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            document = self._accepted(root)
            with mock.patch.object(
                runtime_closure,
                "run_groq_audio_audit_durable",
                side_effect=AssertionError("duplicate Groq audio audit must not run"),
            ):
                result = runtime_closure.run_post_gold_observers(root)
            self.assertEqual(result, document)

    def test_byte_drift_invalidates_pre_gold_contract_for_post_gold_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._accepted(root)
            with (root / "final.mp4").open("ab") as handle:
                handle.write(b"drift")
            expected = {"schema_version": 1, "decision": "legacy-observer"}
            with mock.patch.object(
                runtime_closure,
                "run_groq_audio_audit_durable",
                return_value=expected,
            ) as observer:
                result = runtime_closure.run_post_gold_observers(root)
            self.assertEqual(result, expected)
            observer.assert_called_once()

    def test_blocked_contract_never_suppresses_legacy_observer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._accepted(root, decision="block")
            expected = {"schema_version": 1, "decision": "legacy-observer"}
            with mock.patch.object(
                runtime_closure,
                "run_groq_audio_audit_durable",
                return_value=expected,
            ) as observer:
                result = runtime_closure.run_post_gold_observers(root)
            self.assertEqual(result, expected)
            observer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
