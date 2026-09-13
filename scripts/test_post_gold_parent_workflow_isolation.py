from __future__ import annotations

import unittest
from pathlib import Path


class PostGoldParentWorkflowIsolationTests(unittest.TestCase):
    def test_parent_workflow_has_no_provider_or_piper_work_after_gold(self) -> None:
        path = Path('.github/workflows/resume-gold-qc-pending.yml')
        text = path.read_text(encoding='utf-8')
        marker = '- name: Execute current certified Gold over exact source final'
        self.assertIn(marker, text)
        post_gold = text.split(marker, 1)[1]

        forbidden = (
            'Prepare sibling Short runtime when approved',
            'Rematerialize post-Gold provider capabilities',
            'post-gold-secrets',
            'PIPER_MODEL_PATH',
            'python -m piper.download_voices',
        )
        # GEMINI_TTS_MODEL is deliberately not forbidden: the marker split happens right
        # after the Gold step's own `- name:` line, so this slice still includes that
        # step's own env block. install_production_model_contract() requires
        # GEMINI_TTS_MODEL as a blanket model-identity pin shared by every Gold caller,
        # even though execute_gold_resume() never actually invokes TTS -- its presence
        # here is a required contract value, not regained TTS provider work.
        self.assertIn('GEMINI_TTS_MODEL', post_gold)
        for value in forbidden:
            self.assertNotIn(value, post_gold, f'parent post-Gold path regained external provider work: {value}')

        self.assertIn('Finish exact approved scope after Gold without provider capabilities', post_gold)
        self.assertIn('master-lock.json', post_gold)
        self.assertIn('sibling-short-deferred.json', post_gold)
        self.assertIn('resume-deferred-sibling-shorts.yml', post_gold)
        self.assertIn('continue-on-error: true', post_gold)

    def test_derivative_workflow_owns_provider_backed_child_production(self) -> None:
        text = Path('.github/workflows/resume-deferred-sibling-shorts.yml').read_text(encoding='utf-8')
        self.assertIn('Produce Deferred Sibling Shorts', text)
        self.assertIn('run_deferred_sibling_shorts_v1.py', text)
        self.assertIn('PIPER_MODEL_PATH', text)
        self.assertIn('GEMINI_API_KEY_FILE', text)
        self.assertIn('GROQ_API_KEY_FILE', text)
        self.assertIn('PEXELS_API_KEY_FILE', text)
        self.assertIn('parent_release_tag', text)
        self.assertNotIn("--pattern 'final.mp4'", text)
        self.assertIn('Parent long-form Release is immutable', text)


if __name__ == '__main__':
    unittest.main()
