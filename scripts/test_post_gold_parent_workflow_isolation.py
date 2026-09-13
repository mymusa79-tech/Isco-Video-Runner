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

    def test_reservation_never_consumed_before_gold_is_always_recovered(self) -> None:
        # A run that dies before "Consume exact Gold resume authorization" (e.g. the
        # certification gate or Engine pin verify/install step failing first) must
        # still get its dispatch_reserved queue item marked failed, or every later
        # "تابع Gold" is refused as already in progress with nothing actually running.
        text = Path('.github/workflows/resume-gold-qc-pending.yml').read_text(encoding='utf-8')
        checkout_marker = '- name: Checkout latest encrypted Telegram control state'
        consume_marker = '- name: Consume exact Gold resume authorization'
        recovery_marker = '- name: Mark failed Gold resume reservation when never consumed'
        self.assertIn(checkout_marker, text)
        self.assertIn(consume_marker, text)
        self.assertIn(recovery_marker, text)

        # The control-state checkout must run even when an earlier step already failed,
        # so the recovery step below has an authenticated clone to push through.
        checkout_block = text.split(checkout_marker, 1)[1].split(consume_marker, 1)[0]
        self.assertIn('if: always()', checkout_block)

        recovery_block = text.split(recovery_marker, 1)[1]
        self.assertIn("if: always() && steps.consume.outcome != 'success'", recovery_block)
        self.assertIn('continue-on-error: true', recovery_block)
        self.assertIn('mark_gold_resume_failed', recovery_block)
        # Must key off the raw workflow_dispatch inputs, not steps.consume.outputs --
        # those outputs never exist when consume itself is the step that failed/skipped.
        self.assertIn('REQUEST_ID: ${{ inputs.request_id }}', recovery_block)
        self.assertIn('AUTHORIZATION_ID: ${{ inputs.authorization_id }}', recovery_block)

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
