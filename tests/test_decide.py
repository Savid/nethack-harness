import json
import os
import tempfile
import unittest

from helpers import Case, Endpoint, FakeGame, facts, nh, screen, view  # noqa: F401


class DecideTest(Case):
    def test_request_shape_and_normalised_answers(self):
        ep = Endpoint(lambda body: {"act": {"type": "choice", "choice": "b", "probabilities": {"a": 0.2, "b": 0.8}},
                                    "danger": {"type": "noul", "noul": "0.1"}})
        try:
            out = nh.normalize(nh.decider(ep.url)({"state": {"x": 1}, "questions": {"q": {"type": "noul"}}})["answers"])
            self.assertEqual(set(ep.requests[0]), {"state", "questions"})
            self.assertEqual(out["act"]["confidence"], 0.8)
            self.assertEqual(out["danger"]["noul"], 0.1)
            nh.decider(ep.url, model="m")({"state": 1, "questions": {}})
            self.assertEqual(ep.requests[1]["model"], "m")
        finally:
            ep.close()

    def test_noul_confidence_and_probabilities(self):
        out = nh.normalize({"q": {"type": "noul", "noul": 0.2}, "r": {"type": "noul", "probabilities": {"true": 0.9,
                                                                                                    "false": 0.1}}})
        self.assertAlmostEqual(out["q"]["confidence"], 0.8)
        self.assertAlmostEqual(out["r"]["noul"], 0.9)

    def test_hung_endpoint_fails_fast(self):
        import socket
        import time
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        decide = nh.decider("http://127.0.0.1:%d/v1/systemone" % srv.getsockname()[1])
        t = time.monotonic()
        with self.assertRaises(nh.decide.Unhealthy):
            decide({"state": 1, "questions": {}}, timeout=0.5)
        self.assertLess(time.monotonic() - t, 3)
        srv.close()

    def test_choice_without_probabilities(self):
        out = nh.normalize({"pick": {"type": "choice", "choice": "x", "confidence": 0.7}})
        self.assertEqual(out["pick"]["probabilities"], {"x": 0.7})


if __name__ == "__main__":
    unittest.main()
