from __future__ import annotations

import unittest
from unittest.mock import patch

import isco_video_agent.resilient_planner as staged
import scripts.append_retry_guard as append_guard
from scripts.attempt10_append_bound_recovery import (
    _FIRST_PASS_UNDERFLOOR,
    _carry_whole_sentences,
    _recoverable_first_pass_split,
    _trim_whole_sentences_to_fit,
    install_attempt10_append_bound_recovery,
)


class Attempt10AppendBoundRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_split = append_guard._split_held_and_underfloor_additions
        self.original_validator = append_guard._validate_addition_bounds
        self.retry_token = append_guard._RETRY_ATTEMPTED.set(False)
        self.carry_token = _FIRST_PASS_UNDERFLOOR.set({})

    def tearDown(self) -> None:
        append_guard._split_held_and_underfloor_additions = self.original_split
        append_guard._validate_addition_bounds = self.original_validator
        append_guard._RETRY_ATTEMPTED.reset(self.retry_token)
        _FIRST_PASS_UNDERFLOOR.reset(self.carry_token)

    @staticmethod
    def _spec() -> dict:
        return {
            "id": "sec_8",
            "current_words": 100,
            "hard_section_band": [110, 170],
            "minimum_append_words": 16,
            "maximum_append_words": 35,
        }

    def test_attempt10_exact_69_over_35_is_discarded_for_completion(self) -> None:
        held, retry_ids = _recoverable_first_pass_split(
            {"sec_8": " ".join(["إضافة"] * 69)},
            [self._spec()],
            aggregate_headroom=200,
        )
        self.assertEqual(held, {})
        self.assertEqual(retry_ids, ["sec_8"])
        self.assertEqual(_FIRST_PASS_UNDERFLOOR.get(), {})

    def test_underfloor_first_pass_is_stashed_only_for_completion_carry(self) -> None:
        held, retry_ids = _recoverable_first_pass_split(
            {"sec_8": "هذه إضافة قصيرة. لكنها مفيدة ومترابطة."},
            [self._spec()],
            aggregate_headroom=200,
        )
        self.assertEqual(held, {})
        self.assertEqual(retry_ids, ["sec_8"])
        self.assertEqual(
            _FIRST_PASS_UNDERFLOOR.get(),
            {"sec_8": "هذه إضافة قصيرة. لكنها مفيدة ومترابطة."},
        )

    def test_hard_band_safe_first_pass_addition_is_held(self) -> None:
        text = " ".join(["إضافة"] * 16)
        held, retry_ids = _recoverable_first_pass_split(
            {"sec_8": text},
            [self._spec()],
            aggregate_headroom=200,
        )
        self.assertEqual(held, {"sec_8": text})
        self.assertEqual(retry_ids, [])
        self.assertEqual(_FIRST_PASS_UNDERFLOOR.get(), {})

    def test_whole_sentence_carry_never_truncates_to_fit(self) -> None:
        combined = _carry_whole_sentences(
            "واحد اثنان ثلاثة أربعة خمسة",
            "ستة سبعة ثمانية. تسعة عشرة أحد عشر.",
            required_extra_words=3,
            maximum_append_words=8,
        )
        self.assertEqual(
            combined,
            "واحد اثنان ثلاثة أربعة خمسة ستة سبعة ثمانية.",
        )
        self.assertIsNone(
            _carry_whole_sentences(
                "واحد اثنان ثلاثة أربعة خمسة",
                "ستة سبعة ثمانية تسعة عشرة أحد عشر",
                required_extra_words=3,
                maximum_append_words=8,
            )
        )

    def test_first_pass_oversize_uses_only_existing_completion_call(self) -> None:
        install_attempt10_append_bound_recovery()
        counts = [90] * 8
        sections = [
            staged.ScriptSection(
                id=f"sec_{index}",
                narration=" ".join([f"كلمة{index}"] * words),
                visual_query="room notebook",
                key_point=f"key point {index}",
            )
            for index, words in enumerate(counts, start=1)
        ]
        original = [section.narration for section in sections]
        calls: list[str] = []

        def fake_json(api_key, prompt, model):
            del api_key, model
            calls.append(prompt)
            if len(calls) == 1:
                additions = [
                    {
                        "id": f"sec_{index}",
                        "append_text": " ".join(["إضافة"] * (69 if index == 8 else 26)),
                    }
                    for index in range(1, 9)
                ]
                return {"additions": additions}
            self.assertIn('"sec_8"', prompt)
            return {
                "additions": [
                    {"id": "sec_8", "append_text": " ".join(["إضافة"] * 26)}
                ]
            }

        with patch.object(staged, "json_text", side_effect=fake_json):
            additions = append_guard._repair_all_residual_underlength(
                "key",
                topic="topic",
                model="model",
                sections=sections,
                policy_json="{}",
                research_json="{}",
                narrative_format="problem_reveal_solution",
                current_words=720,
                minimum=800,
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual([section.narration for section in sections], original)
        self.assertEqual(append_guard._word_count(additions["sec_8"]), 26)
        append_guard.staged._append_retry_additions(sections, additions)
        self.assertTrue(all(110 <= append_guard._word_count(s.narration) <= 170 for s in sections))
        self.assertLessEqual(sum(append_guard._word_count(s.narration) for s in sections), 1450)

    def test_run71_underfloor_completion_reuses_discarded_whole_sentence_without_third_call(self) -> None:
        install_attempt10_append_bound_recovery()
        sections = [
            staged.ScriptSection(
                id=f"sec_{index}",
                narration=" ".join([f"كلمة{index}"] * 90),
                visual_query="room notebook",
                key_point=f"key point {index}",
            )
            for index in range(1, 9)
        ]
        original = [section.narration for section in sections]
        calls: list[str] = []

        def fake_json(api_key, prompt, model):
            del api_key, model
            calls.append(prompt)
            if len(calls) == 1:
                additions = []
                for index in range(1, 9):
                    if index == 5:
                        # 18 words: 90 + 18 = 108, genuinely below the 110 hard floor.
                        # The first whole sentence is exactly 14 words, enough to carry
                        # the short completion from 96 to 110 without a third call.
                        text = (
                            "هذا شرح أول يضيف زاوية واضحة تربط الفكرة مباشرة بسلوك يومي يمكن ملاحظته بوضوح. "
                            "وهذه جملة أخرى مفيدة."
                        )
                    else:
                        text = " ".join(["إضافة"] * 26)
                    additions.append({"id": f"sec_{index}", "append_text": text})
                return {"additions": additions}

            self.assertEqual(len(calls), 2)
            self.assertIn('"sec_5"', prompt)
            return {
                "additions": [
                    {
                        "id": "sec_5",
                        "append_text": "ثان شرح مستقل لكنه قصير جدًا",
                    }
                ]
            }

        with patch.object(staged, "json_text", side_effect=fake_json):
            additions = append_guard._repair_all_residual_underlength(
                "key",
                topic="topic",
                model="model",
                sections=sections,
                policy_json="{}",
                research_json="{}",
                narrative_format="problem_reveal_solution",
                current_words=720,
                minimum=800,
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual([section.narration for section in sections], original)
        self.assertGreaterEqual(90 + append_guard._word_count(additions["sec_5"]), 110)
        self.assertLessEqual(append_guard._word_count(additions["sec_5"]), 44)
        append_guard.staged._append_retry_additions(sections, additions)
        self.assertTrue(all(110 <= append_guard._word_count(s.narration) <= 170 for s in sections))
        self.assertLessEqual(sum(append_guard._word_count(s.narration) for s in sections), 1450)

    def test_completion_response_still_fails_closed_when_oversize(self) -> None:
        install_attempt10_append_bound_recovery()
        sections = [
            staged.ScriptSection(
                id=f"sec_{index}",
                narration=" ".join([f"كلمة{index}"] * 90),
                visual_query="room notebook",
                key_point=f"key point {index}",
            )
            for index in range(1, 9)
        ]
        calls = 0

        def fake_json(api_key, prompt, model):
            nonlocal calls
            del api_key, prompt, model
            calls += 1
            if calls == 1:
                return {
                    "additions": [
                        {
                            "id": f"sec_{index}",
                            "append_text": " ".join(["إضافة"] * (18 if index == 8 else 26)),
                        }
                        for index in range(1, 9)
                    ]
                }
            return {
                "additions": [
                    {
                        "id": "sec_8",
                        "append_text": " ".join(["إضافة"] * 75),
                    }
                ]
            }

        with patch.object(staged, "json_text", side_effect=fake_json):
            with self.assertRaisesRegex(RuntimeError, "maximum allowed"):
                append_guard._repair_all_residual_underlength(
                    "key",
                    topic="topic",
                    model="model",
                    sections=sections,
                    policy_json="{}",
                    research_json="{}",
                    narrative_format="problem_reveal_solution",
                    current_words=720,
                    minimum=800,
                )
        self.assertEqual(calls, 2)

    def test_trim_whole_sentences_drops_only_trailing_sentence_to_fit(self) -> None:
        first = " ".join(["إضافة"] * 19) + " اولى."
        second = " ".join(["زياده"] * 19) + " ثانية."
        trimmed = _trim_whole_sentences_to_fit(
            f"{first} {second}",
            current_words=100,
            section_minimum=110,
            maximum_append_words=35,
        )
        self.assertEqual(trimmed, first)

    def test_trim_whole_sentences_fails_closed_when_result_would_drop_under_floor(self) -> None:
        short_first = " ".join(["زياده"] * 4) + " قصيرة."
        filler_second = " ".join(["كبيرة"] * 39) + " اخيرة."
        trimmed = _trim_whole_sentences_to_fit(
            f"{short_first} {filler_second}",
            current_words=100,
            section_minimum=110,
            maximum_append_words=35,
        )
        self.assertIsNone(trimmed)

    def test_trim_whole_sentences_fails_closed_without_a_sentence_boundary(self) -> None:
        trimmed = _trim_whole_sentences_to_fit(
            " ".join(["إضافة"] * 75),
            current_words=90,
            section_minimum=110,
            maximum_append_words=35,
        )
        self.assertIsNone(trimmed)

    def test_run84_completion_over_max_trims_whole_trailing_sentences(self) -> None:
        """Regression for Run #84: sec_8 was discarded on the first pass for
        over_max_append, then its one completion-round response came back 2 words
        over the same maximum_append_words with no existing recovery. When the
        oversize response has a trailing whole sentence that can be dropped without
        pushing the section back under the hard floor, the guard now trims it in
        place instead of failing the whole repair."""
        install_attempt10_append_bound_recovery()
        spec = self._spec()
        first = " ".join(["إضافة"] * 19) + " اولى."
        second = " ".join(["زياده"] * 19) + " ثانية."
        additions = {"sec_8": f"{first} {second}"}

        append_guard._validate_addition_bounds(additions, [spec], aggregate_headroom=200)

        self.assertEqual(additions["sec_8"], first)
        self.assertLessEqual(append_guard._word_count(additions["sec_8"]), spec["maximum_append_words"])

    def test_run84_completion_over_max_trim_fails_closed_when_result_would_underfloor(self) -> None:
        install_attempt10_append_bound_recovery()
        spec = self._spec()
        short_first = " ".join(["زياده"] * 4) + " قصيرة."
        filler_second = " ".join(["كبيرة"] * 39) + " اخيرة."
        additions = {"sec_8": f"{short_first} {filler_second}"}

        with self.assertRaisesRegex(RuntimeError, "maximum allowed"):
            append_guard._validate_addition_bounds(additions, [spec], aggregate_headroom=200)

    def test_run98_narrow_indivisible_overage_is_accepted_as_is(self) -> None:
        """Regression for Run #98: sec_5's one completion-round response came back as
        a single indivisible sentence (no sentence boundary the trimmer could drop)
        at 80 words against a maximum_append_words of 79 - exactly 1 word over. The
        resulting section (100 + 80 = 180... use a spec where it still clears the
        hard maximum) is accepted as-is instead of failing the whole repair."""
        install_attempt10_append_bound_recovery()
        spec = dict(self._spec())
        spec["current_words"] = 90
        spec["hard_section_band"] = [110, 170]
        spec["maximum_append_words"] = 79
        text = " ".join(["إضافة"] * 80)  # one indivisible run, no sentence boundary
        additions = {"sec_8": text}

        append_guard._validate_addition_bounds(additions, [spec], aggregate_headroom=200)

        self.assertEqual(additions["sec_8"], text)
        self.assertEqual(append_guard._word_count(additions["sec_8"]), 80)

    def test_run98_narrow_overage_still_fails_closed_if_it_would_exceed_hard_section_maximum(self) -> None:
        install_attempt10_append_bound_recovery()
        spec = dict(self._spec())
        spec["current_words"] = 100
        spec["hard_section_band"] = [110, 170]
        spec["maximum_append_words"] = 71
        # 100 + 72 = 172 > hard maximum 170: a 1-word overage that would still
        # breach the real hard section band must still fail closed.
        text = " ".join(["إضافة"] * 72)
        additions = {"sec_8": text}

        with self.assertRaisesRegex(RuntimeError, "maximum allowed"):
            append_guard._validate_addition_bounds(additions, [spec], aggregate_headroom=200)

    def test_run98_narrow_overage_tolerance_does_not_extend_to_large_overages(self) -> None:
        install_attempt10_append_bound_recovery()
        spec = self._spec()
        # 75 words against a maximum of 35 is far outside the narrow tolerance and
        # has no sentence boundary at all - must still fail closed exactly as before.
        additions = {"sec_8": " ".join(["إضافة"] * 75)}

        with self.assertRaisesRegex(RuntimeError, "maximum allowed"):
            append_guard._validate_addition_bounds(additions, [spec], aggregate_headroom=200)

    def test_accept_narrow_indivisible_overage_helper_boundaries(self) -> None:
        from scripts.attempt10_append_bound_recovery import _accept_narrow_indivisible_overage

        # Exactly at the tolerance boundary (2 words over) and within section maximum.
        self.assertTrue(
            _accept_narrow_indivisible_overage(
                " ".join(["كلمة"] * 37),
                current_words=100,
                section_maximum=170,
                maximum_append_words=35,
            )
        )
        # One word past the tolerance boundary.
        self.assertFalse(
            _accept_narrow_indivisible_overage(
                " ".join(["كلمة"] * 38),
                current_words=100,
                section_maximum=170,
                maximum_append_words=35,
            )
        )
        # Within tolerance but breaches the true hard section maximum.
        self.assertFalse(
            _accept_narrow_indivisible_overage(
                " ".join(["كلمة"] * 37),
                current_words=134,
                section_maximum=170,
                maximum_append_words=35,
            )
        )
        # Not actually over the budget at all.
        self.assertFalse(
            _accept_narrow_indivisible_overage(
                " ".join(["كلمة"] * 35),
                current_words=100,
                section_maximum=170,
                maximum_append_words=35,
            )
        )
