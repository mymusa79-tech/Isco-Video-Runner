"""Long film gets a longer Produce budget; short/podcast keep 85 minutes."""
import re
import unittest
from pathlib import Path

TEXT = Path(".github/workflows/clean-v2-telegram-production.yml").read_text(encoding="utf-8")


class ProductionTimeoutByScope(unittest.TestCase):
    def test_job_ceiling_covers_long_budget_plus_cleanup(self):
        job = int(re.search(r"^    timeout-minutes: (\d+)$", TEXT, re.M).group(1))
        self.assertGreaterEqual(job, 180)

    def test_produce_step_budget_depends_on_scope(self):
        block = TEXT.split("- name: Produce exact approved Telegram scope", 1)[1].split("env:", 1)[0]
        self.assertIn("env.TELEGRAM_REQUEST_SCOPE == 'long'", block)
        self.assertIn("env.TELEGRAM_REQUEST_SCOPE == 'bundle'", block)
        self.assertIn("&& 160 || 85", block)

    def test_scope_is_exported_before_produce_step(self):
        self.assertLess(TEXT.index('echo "TELEGRAM_REQUEST_SCOPE=$scope" >> "$GITHUB_ENV"'),
                        TEXT.index("- name: Produce exact approved Telegram scope"))


if __name__ == "__main__":
    unittest.main()
