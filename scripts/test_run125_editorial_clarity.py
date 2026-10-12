from __future__ import annotations

import inspect
import unittest

from clean_v2 import pipeline, short_format


class Run125ShortEditorialRegressionTests(unittest.TestCase):
    def test_short_planning_prompts_tie_action_to_actual_obstacle(self) -> None:
        brief = {"approved_topic": "كيف تتعامل مع يوم لم تنجز فيه شيئًا؟"}
        prompt = short_format.short_prompt_context(brief, for_planning=True)
        self.assertIn("REAL-WORLD ACTION CHECK", prompt)
        self.assertIn("FIRST concrete step", prompt)
        self.assertIn("actual problem", prompt)
        self.assertIn("genuinely NEW insight", prompt)
        self.assertIn("unfinished day", prompt)

    def test_writer_prompt_requires_hook_body_payoff_depth_without_word_padding(self) -> None:
        brief = {"approved_topic": "كيف تتعامل مع يوم لم تنجز فيه شيئًا؟"}
        prompt = short_format.short_prompt_context(brief)
        self.assertIn("one complete miniature idea", prompt)
        self.assertIn("actual mechanism/contrast", prompt)
        self.assertIn("without padded words", prompt)

    def test_shared_planning_path_warns_against_visual_prop_actions(self) -> None:
        source = inspect.getsource(pipeline)
        self.assertIn("RUN125 REAL-ACTION TEST", source)
        self.assertIn("choose a directly relevant step before locking it", source)


if __name__ == "__main__":
    unittest.main()