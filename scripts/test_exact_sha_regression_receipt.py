from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.exact_sha_regression_receipt import (
    REQUIRED_GREEN_EVIDENCE,
    RegressionReceiptError,
    build_receipt,
    certification_tag,
    validate_receipt,
)


RUNNER_SHA = "1" * 40
ENGINE_SHA = "2" * 40


class ExactSHARegressionReceiptTests(unittest.TestCase):
    def test_build_and_validate_exact_pair(self) -> None:
        payload = build_receipt(RUNNER_SHA, ENGINE_SHA)
        validated = validate_receipt(payload, runner_sha=RUNNER_SHA, engine_sha=ENGINE_SHA)
        self.assertEqual(validated["status"], "green")
        self.assertFalse(validated["production_dispatch_performed"])
        self.assertEqual(validated["certification_tag"], certification_tag(RUNNER_SHA, ENGINE_SHA))
        self.assertEqual(set(validated["evidence"]), set(REQUIRED_GREEN_EVIDENCE))
        self.assertTrue(all(validated["evidence"].values()))

    def test_rejects_non_exact_or_uppercase_sha(self) -> None:
        for bad in ("abc", "A" * 40, "1" * 39, "1" * 41):
            with self.subTest(bad=bad):
                with self.assertRaises(RegressionReceiptError):
                    build_receipt(bad, ENGINE_SHA)

    def test_rejects_expected_identity_mismatch(self) -> None:
        payload = build_receipt(RUNNER_SHA, ENGINE_SHA)
        with self.assertRaises(RegressionReceiptError):
            validate_receipt(payload, runner_sha="3" * 40, engine_sha=ENGINE_SHA)
        with self.assertRaises(RegressionReceiptError):
            validate_receipt(payload, runner_sha=RUNNER_SHA, engine_sha="4" * 40)

    def test_rejects_any_non_green_evidence(self) -> None:
        payload = build_receipt(RUNNER_SHA, ENGINE_SHA)
        for evidence_name in REQUIRED_GREEN_EVIDENCE:
            with self.subTest(evidence=evidence_name):
                changed = copy.deepcopy(payload)
                changed["evidence"][evidence_name] = False
                with self.assertRaises(RegressionReceiptError):
                    validate_receipt(changed)

    def test_rejects_missing_or_extra_evidence(self) -> None:
        missing = build_receipt(RUNNER_SHA, ENGINE_SHA)
        missing["evidence"].pop(REQUIRED_GREEN_EVIDENCE[0])
        with self.assertRaises(RegressionReceiptError):
            validate_receipt(missing)

        extra = build_receipt(RUNNER_SHA, ENGINE_SHA)
        extra["evidence"]["invented_gate"] = True
        with self.assertRaises(RegressionReceiptError):
            validate_receipt(extra)

    def test_rejects_production_dispatch_or_wrong_tag(self) -> None:
        dispatched = build_receipt(RUNNER_SHA, ENGINE_SHA)
        dispatched["production_dispatch_performed"] = True
        with self.assertRaises(RegressionReceiptError):
            validate_receipt(dispatched)

        wrong_tag = build_receipt(RUNNER_SHA, ENGINE_SHA)
        wrong_tag["certification_tag"] = "canonical-full-regression-green-invalid"
        with self.assertRaises(RegressionReceiptError):
            validate_receipt(wrong_tag)

    def test_serialized_receipt_round_trip_is_stable(self) -> None:
        payload = build_receipt(RUNNER_SHA, ENGINE_SHA)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipt.json"
            path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(validate_receipt(loaded), payload)


if __name__ == "__main__":
    unittest.main()
