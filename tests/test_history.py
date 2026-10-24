"""History ring buffer & downsampling."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from vitals.history import History  # noqa: E402


class HistoryTests(unittest.TestCase):
    def test_append_and_window(self):
        h = History(maxlen=100)
        now = time.time()
        for i in range(10):
            h.append(now - 9 + i, {"percent": 50 + i})
        pts = h.window(5)
        self.assertEqual(len(pts), 5)

    def test_series_passthrough_small(self):
        h = History()
        now = time.time()
        for i in range(5):
            h.append(now - i, {"percent": float(i)})
        s = h.series(["percent"], 3600, max_points=100)
        self.assertEqual(len(s["percent"]), 5)

    def test_downsample_bounds(self):
        h = History(maxlen=7200)
        now = time.time()
        for i in range(2000):
            h.append(now - 2000 + i, {"percent": float(i % 100)})
        s = h.series(["percent"], 3600, max_points=100)
        self.assertLessEqual(len(s["percent"]), 100)
        self.assertGreater(len(s["percent"]), 50)

    def test_null_gaps_preserved(self):
        h = History()
        now = time.time()
        h.append(now - 2, {"percent": 10})
        h.append(now - 1, {})  # no value
        s = h.series(["percent"], 60, max_points=100)
        vals = [v for _, v in s["percent"]]
        self.assertIn(None, vals)

    def test_ring_evicts(self):
        h = History(maxlen=3)
        for i in range(10):
            h.append(time.time(), {"percent": i})
        self.assertEqual(len(h), 3)


if __name__ == "__main__":
    unittest.main()
