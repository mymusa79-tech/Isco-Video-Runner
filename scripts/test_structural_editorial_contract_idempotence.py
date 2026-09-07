from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts import structural_editorial_contract as contract


class StructuralEditorialIdempotenceTests(unittest.TestCase):
    def test_install_is_idempotent(self) -> None:
        issue = contract.staged._section_length_issue_notes
        build = contract.staged.build_plan
        try:
            contract.install_structural_editorial_contract()
            first_issue = contract.staged._section_length_issue_notes
            first_build = contract.staged.build_plan
            contract.install_structural_editorial_contract()
            self.assertIs(contract.staged._section_length_issue_notes, first_issue)
            self.assertIs(contract.staged.build_plan, first_build)
        finally:
            contract.staged._section_length_issue_notes = issue
            contract.staged.build_plan = build


if __name__ == "__main__":
    unittest.main()
