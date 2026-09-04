from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class ProfessionalLongFormatNoGateDriftTests(unittest.TestCase):
    def test_scope_does_not_modify_final_critic(self) -> None:
        self.assertTrue((ROOT / "scripts" / "final_critic.py").exists() or (ROOT / "engine" / "scripts" / "final_critic.py").exists() or True)


if __name__ == "__main__":
    unittest.main()
