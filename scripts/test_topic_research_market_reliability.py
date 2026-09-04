from __future__ import annotations

import unittest
from dataclasses import dataclass
from types import SimpleNamespace

from scripts import topic_research_market_reliability as reliability


@dataclass
class Candidate:
    title: str
    channel_fit_score: float = 0.9
    market_query: str = ""
    market_timing_status: str = "unverified"
    source_mode: str = "model_generated"

    def __post_init__(self) -> None:
        if not self.market_query:
            self.market_query = self.title


class TopicResearchMarketReliabilityTests(unittest.TestCase):
    def _pool(self, count: int) -> list[Candidate]:
        return [Candidate(f"topic-{index}") for index in range(count)]

    def test_expands_beyond_first_five_to_reach_three_when_available(self):
        calls: list[list[str]] = []
        live_titles = {"topic-0", "topic-5", "topic-6"}

        def original(_key, candidates, **_kwargs):
            batch = list(candidates)
            calls.append([item.title for item in batch])
            for item in batch:
                if item.title in live_titles:
                    item.market_timing_status = "measured"
                    item.source_mode = "live_research"
                else:
                    item.market_timing_status = "insufficient_evidence"
                    item.source_mode = "live_research_incomplete"
            return batch

        measured = reliability.adaptive_measure_market_timing(
            original, "youtube-key", self._pool(10)
        )
        self.assertEqual([len(batch) for batch in calls], [5, 2])
        self.assertEqual(
            sum(1 for item in measured if reliability._is_live_measured(item)), 3
        )

    def test_stops_after_initial_five_when_target_is_satisfied(self):
        calls: list[int] = []

        def original(_key, candidates, **_kwargs):
            batch = list(candidates)
            calls.append(len(batch))
            for index, item in enumerate(batch):
                if index < 3:
                    item.market_timing_status = "measured"
                    item.source_mode = "live_research"
            return batch

        reliability.adaptive_measure_market_timing(
            original, "youtube-key", self._pool(10)
        )
        self.assertEqual(calls, [5])

    def test_one_live_candidate_survives_after_hard_cap(self):
        calls: list[int] = []

        def original(_key, candidates, **_kwargs):
            batch = list(candidates)
            calls.append(len(batch))
            if batch and batch[0].title == "topic-0":
                batch[0].market_timing_status = "measured"
                batch[0].source_mode = "live_research"
            return batch

        measured = reliability.adaptive_measure_market_timing(
            original, "youtube-key", self._pool(14)
        )
        self.assertEqual(sum(calls), reliability.MAX_MARKET_PROBE_CANDIDATES)
        self.assertEqual(
            sum(1 for item in measured if reliability._is_live_measured(item)), 1
        )

    def test_two_live_candidates_survive_without_lowering_evidence_gate(self):
        live_titles = {"topic-0", "topic-8"}

        def original(_key, candidates, **_kwargs):
            batch = list(candidates)
            for item in batch:
                if item.title in live_titles:
                    item.market_timing_status = "measured"
                    item.source_mode = "live_research"
            return batch

        measured = reliability.adaptive_measure_market_timing(
            original, "youtube-key", self._pool(10)
        )
        self.assertEqual(
            sum(1 for item in measured if reliability._is_live_measured(item)), 2
        )

    def test_zero_live_candidates_is_the_only_shortfall(self):
        calls: list[int] = []

        def original(_key, candidates, **_kwargs):
            batch = list(candidates)
            calls.append(len(batch))
            for item in batch:
                item.market_timing_status = "insufficient_evidence"
                item.source_mode = "live_research_incomplete"
            return batch

        with self.assertRaises(reliability.LiveMarketEvidenceShortfall) as raised:
            reliability.adaptive_measure_market_timing(
                original, "youtube-key", self._pool(10)
            )
        exc = raised.exception
        self.assertEqual(exc.verified, 0)
        self.assertEqual(exc.required, 1)
        self.assertEqual(exc.probes_used, 10)
        self.assertEqual(exc.probe_limit, 10)

    def test_duplicate_identity_does_not_fake_target_but_one_valid_result_survives(self):
        pool = self._pool(5)
        for item in pool[:3]:
            item.market_query = "same-query"

        def original(_key, candidates, **_kwargs):
            batch = list(candidates)
            for item in batch[:3]:
                item.market_timing_status = "measured"
                item.source_mode = "live_research"
            return batch

        measured = reliability.adaptive_measure_market_timing(
            original, "youtube-key", pool
        )
        self.assertEqual(
            len({
                reliability._candidate_identity(item)
                for item in measured
                if reliability._is_live_measured(item)
            }),
            1,
        )

    def test_installer_is_idempotent(self):
        def original(_key, candidates, **_kwargs):
            return list(candidates)

        engine = SimpleNamespace(measure_market_timing=original)
        reliability.install_market_probe_reliability(engine)
        first = engine.measure_market_timing
        reliability.install_market_probe_reliability(engine)
        self.assertIs(engine.measure_market_timing, first)
        self.assertIs(first._isco_original_measure_market_timing, original)

    def test_shortfall_reason_reports_zero_verified_without_relaxing_evidence(self):
        core = SimpleNamespace(_research_failure_reason=lambda _exc: "legacy")
        reliability.install_shortfall_reason(core)
        reason = core._research_failure_reason(
            reliability.LiveMarketEvidenceShortfall(0, 1, 10, 10)
        )
        self.assertIn("أي فرصة", reason)
        self.assertIn("10", reason)
        self.assertIn("لن أعرض رقم اهتمام حالي غير موثق", reason)


if __name__ == "__main__":
    unittest.main()
