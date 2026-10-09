import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    "clean-v2-short-final-one",
    "clean-v2-podcast-one",
    "clean-v2-telegram-production",
)


class ResumeAcrossRunnerEnv(unittest.TestCase):
    def test_flag_is_exactly_one(self):
        for name in WORKFLOWS:
            data = yaml.safe_load((ROOT / ".github/workflows" / f"{name}.yml").read_text())
            values = [
                job.get("env", {}).get("CLEAN_V2_RESUME_ACROSS_RUNNER")
                for job in data["jobs"].values()
            ]
            self.assertIn("1", values, name)


if __name__ == "__main__":
    unittest.main()
