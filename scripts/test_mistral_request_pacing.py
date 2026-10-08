import unittest
from unittest.mock import patch

from clean_v2 import mistral_executor as mx


class PacingTests(unittest.TestCase):
    def setUp(self):
        mx._LAST_REQUEST_AT[0] = 0.0

    def test_second_request_within_interval_waits(self):
        clock = [100.0]
        slept = []

        def sleep(sec):
            slept.append(sec)
            clock[0] += sec

        with patch.object(mx.time, "monotonic", lambda: clock[0]), patch.object(mx.time, "sleep", sleep):
            mx._pace_request()
            clock[0] += 0.46
            mx._pace_request()
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], mx.MISTRAL_MIN_REQUEST_INTERVAL_SECONDS - 0.46, places=6)

    def test_request_after_interval_does_not_wait(self):
        clock = [100.0]
        slept = []
        with patch.object(mx.time, "monotonic", lambda: clock[0]), patch.object(mx.time, "sleep", slept.append):
            mx._pace_request()
            clock[0] += 5
            mx._pace_request()
        self.assertEqual(slept, [])


if __name__ == "__main__":
    unittest.main()
