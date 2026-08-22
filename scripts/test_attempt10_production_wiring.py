from __future__ import annotations

import unittest

import scripts.append_retry_guard as append_guard
from scripts.attempt9_schema_normalizer import install_attempt9_schema_normalizer


class Attempt10ProductionWiringTests(unittest.TestCase):
    def test_existing_run_v3_installer_chain_installs_bound_recovery(self) -> None:
        original_split = append_guard._split_held_and_underfloor_additions
        try:
            install_attempt9_schema_normalizer()
            self.assertTrue(
                getattr(
                    append_guard._split_held_and_underfloor_additions,
                    "_isco_attempt10_append_bound_recovery",
                    False,
                )
            )
        finally:
            append_guard._split_held_and_underfloor_additions = original_split


if __name__ == "__main__":
    unittest.main()
