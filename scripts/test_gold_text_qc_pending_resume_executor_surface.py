from __future__ import annotations

import inspect
import unittest

from scripts import resume_gold_qc_pending_v1 as resume


class GoldTextResumeExecutorSurfaceTests(unittest.TestCase):
    def test_resume_executor_stays_gold_only(self) -> None:
        source = inspect.getsource(resume.execute_gold_resume)
        self.assertIn("run_gold_enforce_phase4", source)
        self.assertNotIn("produce(", source)
        self.assertNotIn("render", source.lower().replace("rerender_performed", ""))
        self.assertIn('"planning_performed": False', source)
        self.assertIn('"visual_retrieval_performed": False', source)
        self.assertIn('"tts_performed": False', source)
        self.assertIn('"rerender_performed": False', source)
        self.assertIn('"final_master_qc_reexecuted": False', source)


if __name__ == "__main__":
    unittest.main()
