from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import production_failure_diagnostics as failure_diag
from scripts.text_audit_provider_mesh import TextAuditUnavailableError


class ProductionFailureDiagnosticsTests(unittest.TestCase):
    def test_existing_text_audit_unavailable_owner_is_classified_as_technical(self):
        exc = TextAuditUnavailableError(
            "TEXT_AUDIT_UNAVAILABLE dimensions=tone action=fail_closed_without_content_repair"
        )
        self.assertFalse(failure_diag.is_tone_semantic_failure(exc))
        self.assertEqual(
            failure_diag.classify_production_failure(exc),
            ("text_audit", "TEXT_AUDIT_UNAVAILABLE"),
        )

    def test_real_tone_gate_remains_identifiable(self):
        exc = RuntimeError("Independent tone/naturalness gate blocked real production")
        self.assertTrue(failure_diag.is_tone_semantic_failure(exc))
        self.assertEqual(
            failure_diag.classify_production_failure(exc),
            ("text_audit", "TONE_SEMANTIC_BLOCK"),
        )

    def test_failure_file_is_secret_free_and_does_not_persist_raw_message(self):
        secret = "SECRET-DO-NOT-PERSIST"
        exc = RuntimeError(f"429 provider URL key={secret}")
        with tempfile.TemporaryDirectory() as td:
            path = failure_diag.write_production_failure_diagnostics(Path(td), exc)
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        self.assertNotIn(secret, raw)
        self.assertFalse(payload["raw_exception_persisted"])
        self.assertEqual(payload["category"], "provider")
        self.assertEqual(payload["error_code"], "PROVIDER_CAPACITY_FAILURE")

    def test_visual_failure_is_not_mislabeled_tone(self):
        exc = RuntimeError("visual selection failed after bounded candidates")
        self.assertFalse(failure_diag.is_tone_semantic_failure(exc))
        self.assertEqual(
            failure_diag.classify_production_failure(exc),
            ("visual", "VISUAL_FAILURE"),
        )


if __name__ == "__main__":
    unittest.main()
