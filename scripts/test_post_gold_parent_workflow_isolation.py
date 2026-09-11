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
            'GEMINI_TTS_MODEL',
            'python -m piper.download_voices',
        )
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
