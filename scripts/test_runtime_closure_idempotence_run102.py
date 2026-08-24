from __future__ import annotations

import unittest

import scripts.run_v3_voice as production
from scripts import runtime_closure, runtime_reliability


class RuntimeClosureIdempotenceRun102Tests(unittest.TestCase):
    def test_manifest_guards_do_not_stack_when_runtime_closure_is_reinstalled(self) -> None:
        original_manifest = production._write_production_manifest
        original_gold = production.run_gold_enforce_phase4

        def base_manifest(out, *, production_id, fmt):
            del out, production_id
            return {"format": fmt}

        def base_gold(*args, **kwargs):
            del args, kwargs
            return None

        production._write_production_manifest = base_manifest
        production.run_gold_enforce_phase4 = base_gold
        try:
            runtime_closure.install_canonical_v4_bundle_post_manifest()
            runtime_reliability.install_release_transaction_guard()

            first_manifest = production._write_production_manifest
            first_canonical = getattr(first_manifest, "_isco_release_transaction_original", None)
            self.assertTrue(getattr(first_manifest, "_isco_release_transaction_delivery", False))
            self.assertTrue(getattr(first_canonical, "_isco_canonical_v4_bundle", False))
            self.assertIs(getattr(first_canonical, "_isco_canonical_v4_original", None), base_manifest)
            self.assertTrue(
                runtime_reliability.manifest_wrapper_chain_has_marker(
                    first_manifest, "_isco_canonical_v4_bundle"
                )
            )

            runtime_closure.install_canonical_v4_bundle_post_manifest()
            runtime_reliability.install_release_transaction_guard()

            self.assertIs(production._write_production_manifest, first_manifest)
            self.assertIs(
                getattr(production._write_production_manifest, "_isco_release_transaction_original", None),
                first_canonical,
            )
            self.assertIs(getattr(first_canonical, "_isco_canonical_v4_original", None), base_manifest)
        finally:
            production._write_production_manifest = original_manifest
            production.run_gold_enforce_phase4 = original_gold


if __name__ == "__main__":
    unittest.main()
