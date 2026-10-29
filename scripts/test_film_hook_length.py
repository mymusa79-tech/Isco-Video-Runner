"""The film prayer waits for the whole hook (>=50 words, <=4 sentences), not one sentence."""
import unittest

from clean_v2 import identity_sequence as identity

S = ["واحد " * 13 + "نهاية.", "اثنان " * 15 + "نهاية.", "ثلاثة " * 12 + "نهاية.", "اربعة " * 11 + "نهاية.", "خمسة كلمات اخرى هنا."]


class FilmHookLength(unittest.TestCase):
    def test_hook_stops_at_fifty_words(self):
        hook = identity._film_opening_hook(" ".join(S))
        self.assertGreaterEqual(len(hook.split()), 50)
        self.assertTrue(hook.endswith("نهاية."))
        self.assertNotIn("خمسة كلمات", hook)

    def test_four_sentence_cap(self):
        short = " ".join(["جملة قصيرة جدا."] * 8)
        self.assertEqual(identity._film_opening_hook(short), " ".join(["جملة قصيرة جدا."] * 4))

    def test_one_long_sentence_is_the_hook(self):
        text = "كلمة " * 60 + "نهاية. جملة ثانية."
        self.assertTrue(identity._film_opening_hook(text).endswith("نهاية."))

    def test_prayer_is_inserted_after_hook_and_order_asserts(self):
        sections = [{"id": "s1", "narration": " ".join(S)}, {"id": "s2", "narration": "قسم ثان."}]
        identity.inject_spoken_identity(sections, fmt="film", opener="", closer="")
        identity.assert_spoken_identity(sections, fmt="film", opener="", closer="")
        narration = sections[0]["narration"]
        before = narration[: narration.index(identity.PRAYER_SENTENCE)]
        self.assertGreaterEqual(len(before.split()), 50)

    def test_short_and_podcast_hooks_unchanged(self):
        self.assertEqual(identity._opening_hook_for("short", "أ. ب. ج."), "أ. ب.")
        self.assertEqual(identity._opening_hook_for("podcast", "أ. ب."), "أ.")


if __name__ == "__main__":
    unittest.main()
