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

    def test_expands_beyond_first_five_only_when_needed(self):
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
            original,
            "youtube-key",
            self._pool(10),
        )

        self.assertEqual([len(batch) for batch in calls], [5, 2])
        self.assertEqual(len(measured), 7)
        self.assertEqual(
            sum(1 for item in measured if reliability._is_live_measured(item)),
            3,
        )

    def test_stops_after_initial_five_when_contract_is_already_satisfied(self):
        calls: list[int] = []

        def original(_key, candidates, **_kwargs):
            batch = list(candidates)
            calls.append(len(batch))
            for index, item in enumerate(batch):
                if index < 3:
                    item.market_timing_status = "measured"
                    item.source_mode = "live_research"
            return batch

        reliability.adaptive_measure_market_timing(original, "youtube-key", self._pool(10))
        self.assertEqual(calls, [5])

    def test_never_exceeds_hard_cap_and_reports_structured_progress(self):
        calls: list[int] = []

        def original(_key, candidates, **_kwargs):
            batch = list(candidates)
            calls.append(len(batch))
            if batch and batch[0].title == "topic-0":
                batch[0].market_timing_status = "measured"
                batch[0].source_mode = "live_research"
            return batch

        with self.assertRaises(reliability.LiveMarketEvidenceShortfall) as raised:
            reliability.adaptive_measure_market_timing(
                original,
                "youtube-key",
                self._pool(14),
            )

        exc = raised.exception
        self.assertEqual(sum(calls), reliability.MAX_MARKET_PROBE_CANDIDATES)
        self.assertEqual(exc.verified, 1)
        self.assertEqual(exc.required, 3)
        self.assertEqual(exc.probes_used, 10)
        self.assertEqual(exc.probe_limit, 10)
        self.assertIn("only 1 candidates", str(exc))

    def test_duplicate_market_identity_does_not_fake_three_distinct_live_candidates(self):
        pool = self._pool(5)
        for item in pool[:3]:
            item.market_query = "same-query"

        def original(_key, candidates, **_kwargs):
            batch = list(candidates)
            for item in batch[:3]:
                item.market_timing_status = "measured"
                item.source_mode = "live_research"
            return batch

        with self.assertRaises(reliability.LiveMarketEvidenceShortfall) as raised:
            reliability.adaptive_measure_market_timing(original, "youtube-key", pool)
        self.assertEqual(raised.exception.verified, 1)

    def test_installer_is_idempotent(self):
        def original(_key, candidates, **_kwargs):
            return list(candidates)

        engine = SimpleNamespace(measure_market_timing=original)
        reliability.install_market_probe_reliability(engine)
        first = engine.measure_market_timing
        reliability.install_market_probe_reliability(engine)
        self.assertIs(engine.measure_market_timing, first)
        self.assertIs(first._isco_original_measure_market_timing, original)

    def test_shortfall_reason_exposes_verified_progress_without_relaxing_gate(self):
        core = SimpleNamespace(_research_failure_reason=lambda _exc: "legacy")
        reliability.install_shortfall_reason(core)
        reason = core._research_failure_reason(
            reliability.LiveMarketEvidenceShortfall(1, 3, 10, 10)
        )
        self.assertIn("1/3", reason)
        self.assertIn("10", reason)
        self.assertIn("لن أعرض رقم اهتمام حالي غير موثق", reason)


if __name__ == "__main__":
    unittest.main()
