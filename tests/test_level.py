import json
import os
import tempfile
import unittest

from helpers import Case, Endpoint, FakeGame, facts, nh, screen, view  # noqa: F401


class PathTest(Case):
    def test_frontier_through_doorway(self):
        rows = [" ------",
                " |.@..|",
                " |....",
                " ------"]
        v = view(screen("", rows), (2, 3))
        lv = nh.Level()
        lv.observe(v)
        dist = lv.paths(v, (2, 3))
        self.assertEqual(dist[(2, 5)], 2)
        targets = [p for _, p in lv.frontier(v, dist)]
        self.assertIn((3, 5), targets)   # beside the gap in the east wall

    def test_travel_keys(self):
        self.assertEqual(nh.travel((5, 5), (6, 15)), "_@Lllj.")


if __name__ == "__main__":
    unittest.main()
