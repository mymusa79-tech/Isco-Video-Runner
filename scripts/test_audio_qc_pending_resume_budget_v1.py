from __future__ import annotations

import unittest

from isco_video_agent.ai_budget import BudgetLedger, PROVIDER_ATTEMPT_HARD_CAP
from scripts import resume_audio_qc_pending_v1 as resume


class AudioQCPendingResumeBudgetV1Tests(unittest.TestCase):
    def test_film_resume_uses_only_remaining_historical_hard_cap(self) -> None:
        cap = PROVIDER_ATTEMPT_HARD_CAP["film"]
        self.assertEqual(
            resume._max_audio_resume_attempts("film", cap - 3, 2),
            1,
        )
        self.assertEqual(
            resume._max_audio_resume_attempts("film", cap - 2, 2),
            0,
        )

    def test_source_budget_already_over_hard_cap_fails_closed(self) -> None:
        cap = PROVIDER_ATTEMPT_HARD_CAP["film"]
        with self.assertRaisesRegex(
            resume.AudioQCPendingResumeError,
            "source_provider_budget_already_exceeded",
        ):
            resume._max_audio_resume_attempts("film", cap, 1)

    def test_moment_keeps_audio_local_two_attempt_bound_when_no_engine_run_cap_exists(self) -> None:
        self.assertIsNone(PROVIDER_ATTEMPT_HARD_CAP.get("moment"))
        self.assertEqual(resume._max_audio_resume_attempts("moment", 100, 2), 2)

    def test_preloaded_historical_attempts_are_seen_by_gold_ledger_authorization(self) -> None:
        cap = PROVIDER_ATTEMPT_HARD_CAP["film"]
        ledger = BudgetLedger("film", enforce=True)
        resume._preload_provider_attempt_count(
            ledger,
            count=cap,
            provider="historical-production",
            prefix="SOURCE",
        )
        self.assertEqual(ledger.to_summary()["provider_attempts"]["total"], cap)


if __name__ == "__main__":
    unittest.main()
