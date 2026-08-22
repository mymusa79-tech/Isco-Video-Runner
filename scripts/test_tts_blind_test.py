from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import tts_blind_test as blind


CORPUS = Path(__file__).with_name("tts_blind_corpus_ar.json")


class TtsBlindHarnessTests(unittest.TestCase):
    def test_fixed_corpus_has_exactly_ten_unique_arabic_samples(self) -> None:
        rows = blind._read_corpus(CORPUS)
        self.assertEqual(len(rows), 10)
        self.assertEqual(len({row.sample_id for row in rows}), 10)
        self.assertTrue(all(row.text and row.direction for row in rows))
        self.assertTrue(all(any("\u0600" <= char <= "\u06ff" for char in row.text) for row in rows))

    def test_rubric_has_eight_dimensions_and_weights_sum_to_100(self) -> None:
        self.assertEqual(len(blind.RUBRIC), 8)
        self.assertEqual(sum(blind.RUBRIC.values()), 100)
        self.assertEqual(set(blind.ENGINES), {"gemini", "voxcpm2", "chatterbox_multilingual_v3"})

    def test_blind_order_is_deterministic_and_contains_each_engine_once(self) -> None:
        first = blind._blind_order("ar01-calm-opening")
        second = blind._blind_order("ar01-calm-opening")
        self.assertEqual(first, second)
        self.assertEqual(set(first), set(blind.ENGINES))
        self.assertEqual(len(first), 3)

    def test_generate_produces_raw_blind_mapping_and_unscored_template(self) -> None:
        def adapter_for(engine):
            def fake(item, output):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes((engine + item.sample_id).encode("utf-8") * 80)
            return fake

        with tempfile.TemporaryDirectory() as td, patch.object(blind, "_adapter", side_effect=adapter_for):
            root = Path(td) / "blind-run"
            result = blind.generate(CORPUS, root)
            self.assertEqual(len(result["mapping"]), 10)
            mapping = json.loads((root / "blind-mapping.json").read_text(encoding="utf-8"))
            template = json.loads((root / "scores-template.json").read_text(encoding="utf-8"))
            self.assertFalse(mapping["production_decision_frozen"])
            self.assertEqual(len(template["samples"]), 10)
            self.assertEqual(len(list((root / "blind").glob("*.wav"))), 30)
            for sample in template["samples"]:
                self.assertEqual(set(sample["scores"]), {"A", "B", "C"})
                for dimensions in sample["scores"].values():
                    self.assertTrue(all(value is None for value in dimensions.values()))

    def test_score_unblinds_weighted_raw_results_without_freezing_voice(self) -> None:
        corpus = blind._read_corpus(CORPUS)
        mapping = {
            row.sample_id: {label: engine for label, engine in zip(("A", "B", "C"), blind.ENGINES)}
            for row in corpus
        }
        scores = blind._rubric_template(corpus, mapping)
        for row in scores["samples"]:
            for label, dimensions in row["scores"].items():
                engine = mapping[row["sample_id"]][label]
                value = {"gemini": 8.0, "voxcpm2": 7.0, "chatterbox_multilingual_v3": 6.0}[engine]
                for key in dimensions:
                    dimensions[key] = value

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            scores_path = root / "scores.json"
            mapping_path = root / "mapping.json"
            result_path = root / "result.json"
            scores_path.write_text(json.dumps(scores, ensure_ascii=False), encoding="utf-8")
            mapping_path.write_text(
                json.dumps({"schema_version": 1, "mapping": mapping}, ensure_ascii=False),
                encoding="utf-8",
            )
            result = blind.score(scores_path, mapping_path, result_path)
            self.assertEqual(result["raw_ranking"], ["gemini", "voxcpm2", "chatterbox_multilingual_v3"])
            self.assertEqual(result["engine_weighted_average_1_to_10"]["gemini"], 8.0)
            self.assertFalse(result["production_decision_frozen"])
            self.assertTrue(result_path.is_file())

    def test_score_rejects_missing_or_out_of_range_human_scores(self) -> None:
        corpus = blind._read_corpus(CORPUS)
        mapping = {
            row.sample_id: {label: engine for label, engine in zip(("A", "B", "C"), blind.ENGINES)}
            for row in corpus
        }
        scores = blind._rubric_template(corpus, mapping)
        scores["samples"][0]["scores"]["A"]["naturalness"] = 11
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = root / "scores.json"
            m = root / "mapping.json"
            s.write_text(json.dumps(scores), encoding="utf-8")
            m.write_text(json.dumps({"mapping": mapping}), encoding="utf-8")
            with self.assertRaises(ValueError):
                blind.score(s, m, root / "result.json")


if __name__ == "__main__":
    unittest.main()
