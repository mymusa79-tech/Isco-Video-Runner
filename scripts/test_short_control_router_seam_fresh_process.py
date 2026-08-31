from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ShortControlRouterSeamFreshProcessTests(unittest.TestCase):
    """Regression for the 2026-08-31 Telegram production outage.

    execute_control_request (Short/Long Telegram control plane) and
    canonical_v4_short_child.execute (auto sibling-Short derivation) used to save and
    restore scripts.run_v3_voice.install_router around production.main(). Commit 148153b
    ("Route V4 planning composition through canonical seam") consolidated that call
    inside scripts.planning_runtime_contract.install_entrypoint_planning_contracts(),
    which resolves install_router from its own module globals - not from an attribute on
    run_v3_voice. That left scripts.run_v3_voice.install_router undefined, so every real
    Telegram-triggered production (Short or Long) crashed with AttributeError before any
    planning logic ran. No existing test called execute_control_request or
    install_entrypoint_planning_contracts together for real, so nothing caught it.

    This test proves both halves for real, in a fresh process (install_entrypoint_
    planning_contracts performs real global monkeypatching that must not leak into the
    rest of the test suite): the dead attribute is gone, and patching
    scripts.planning_runtime_contract.install_router is the interception point that
    production.main()'s call chain actually honors.
    """

    def test_run_v3_voice_has_no_install_router_attribute(self) -> None:
        import scripts.run_v3_voice as production

        self.assertFalse(
            hasattr(production, "install_router"),
            "scripts.run_v3_voice regained an install_router attribute - if this is "
            "intentional, execute_control_request and canonical_v4_short_child.execute "
            "must be re-checked for which name they should patch",
        )

    def test_patching_planning_runtime_contract_install_router_is_actually_honored(self) -> None:
        probe = textwrap.dedent(
            """
            from scripts import planning_runtime_contract

            calls = []

            def fake_short_install():
                calls.append("short_install_called")

            original = planning_runtime_contract.install_router
            planning_runtime_contract.install_router = fake_short_install
            try:
                planning_runtime_contract.install_entrypoint_planning_contracts()
            finally:
                planning_runtime_contract.install_router = original

            assert calls == ["short_install_called"], calls
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)


if __name__ == "__main__":
    unittest.main()
