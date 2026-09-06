from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.producer_quality_contract import ProducerQualityContractError
from scripts.production_failure_diagnostics import write_production_failure_diagnostics


class ProductionFailureDiagnosticsContractTests(unittest.TestCase):
    def test_producer_quality_failure_persists_only_allowlisted_structural_paths(self) -> None:
        exc = ProducerQualityContractError(
            "producer_plan_handoff_blocked:moment_direct_imperative_in_story_beat:"
            "failing_field_paths=hook,sections[0].on_screen_text,untrusted.path"
        )

        with TemporaryDirectory() as root:
            path = write_production_failure_diagnostics(Path(root), exc)
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["category"], "planning")
        self.assertEqual(payload["error_code"], "PRODUCER_PLAN_QUALITY_BLOCK")
        self.assertEqual(
            payload["producer_failing_field_paths"],
            ["hook", "sections[0].on_screen_text"],
        )
        self.assertFalse(payload["raw_exception_persisted"])
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("untrusted.path", serialized)
        self.assertNotIn("moment_direct_imperative_in_story_beat", serialized)

    def test_plain_producer_quality_failure_does_not_invent_field_paths(self) -> None:
        exc = ProducerQualityContractError(
            "producer_plan_handoff_blocked:moment_direct_imperative_in_story_beat"
        )

        with TemporaryDirectory() as root:
            path = write_production_failure_diagnostics(Path(root), exc)
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["category"], "planning")
        self.assertEqual(payload["error_code"], "PRODUCER_PLAN_QUALITY_BLOCK")
        self.assertNotIn("producer_failing_field_paths", payload)
        self.assertFalse(payload["raw_exception_persisted"])


if __name__ == "__main__":
    unittest.main()
