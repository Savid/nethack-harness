from unittest import TestCase
from unittest import mock
import io
from urllib.error import HTTPError
from helpers import Endpoint
from nethack_harness.actions import Action
from nethack_harness.decide import Engine, DecisionError, request_body


class EngineTest(TestCase):
    def test_argument_facts_preserve_choices_modifiers_and_target_evidence(self):
        adjacent = {"direction": "u", "position": [8, 72], "glyph": "#", "remembered_terrain": "#"}
        observation = {"phase": "play", "hero": {"position": [9, 71]}, "adjacent": [adjacent]}
        actions = [Action("move:u", "Move northeast", "move", "u", target=(7, 71)),
                   Action("move:no_pickup:u", "Move northeast without pickup", "move", "mu", target=(7, 71)),
                   Action("open:l", "Open east", "open", "o", target=(8, 71)),
                   Action("pause", "Return control", "pause", steps=0)]
        request = request_body(observation, actions, "Reach [8,72]", tool="move")
        criteria = request["questions"]["action"]["criteria"]
        facts = request["state"]["argument_facts"]
        self.assertEqual(set(criteria), {"move:u", "move:no_pickup:u", "pause"})
        self.assertEqual(set(facts), set(criteria))
        self.assertEqual(request["state"]["observation"], observation)
        self.assertEqual(criteria["move:u"], actions[0].description)
        self.assertEqual(facts["move:u"]["target"], [8, 72])
        self.assertEqual(facts["move:u"]["origin"], [9, 71])
        self.assertEqual(facts["move:u"]["delta"], [-1, 1])
        self.assertEqual(facts["move:u"]["direction_key"], "u")
        self.assertEqual(facts["move:u"]["direction"], "northeast")
        self.assertEqual(facts["move:u"]["target_observation"], adjacent)
        self.assertIsNone(facts["move:u"]["modifier"])
        self.assertEqual(facts["move:no_pickup:u"]["modifier"]["name"], "no_pickup")
        self.assertEqual(facts["pause"]["max_steps"], 0)
        # Vertical and self directions address the same coordinates but differ mechanically.
        actions = [Action("open:" + key, "Open", "open", target=(8, 70)) for key in ".<>"]
        facts = request_body(observation, actions, "Open", tool="open")["state"]["argument_facts"]
        self.assertEqual([facts[a.id]["direction"] for a in actions], ["self", "up", "down"])
        self.assertTrue(all(f["delta"] == [0, 0] for f in facts.values()))
        self.assertTrue(all("target_observation" not in f for f in facts.values()))

    def test_destinations_are_visible_before_selecting_a_navigation_tool(self):
        route = ((2, 4), (2, 5), (2, 6))
        action = Action("travel:3,7", "Travel to down stairs", "travel", steps=2, target=(2, 6), route=route)
        request = request_body({"phase": "play"}, [action], "Explore")
        self.assertEqual(request["state"]["decision"]["stage"], "tool")
        self.assertEqual(request["state"]["navigation"]["destinations"], [
            {"action": "travel:3,7", "description": "Travel to down stairs", "position": [3, 7],
             "known_path_length": 3, "next_position": [3, 5], "max_steps": 2}])

    def test_exact_choice_is_used_without_confidence_gate(self):
        endpoint = Endpoint(lambda _: {"answers": {"action": {"choice": "attack:l", "confidence": 0.01}}})
        self.addCleanup(endpoint.close)
        request = request_body({"hero": {"hp": 1}}, [Action("attack:l", "Attack east", "attack")], "Reach depth 10", tool="attack")
        chosen, reply, seconds = Engine(endpoint.url).choose(request)
        self.assertEqual(chosen, "attack:l")
        self.assertEqual(reply["answers"]["action"]["confidence"], 0.01)
        self.assertEqual(endpoint.requests, [request])
        self.assertGreaterEqual(seconds, 0)

    def test_malformed_and_unavailable_choices_fail(self):
        for response in ({}, {"answers": {"action": {"choice": "missing"}}}, {"answers": {"action": {"choice": 1}}}):
            with self.subTest(response=response):
                endpoint = Endpoint(lambda _, result=response: result)
                try:
                    with self.assertRaises(DecisionError):
                        Engine(endpoint.url).choose(request_body({}, [Action("wait:1", "Wait", "wait")], "Play"))
                finally:
                    endpoint.close()

    def test_http_failure_is_reported_without_retrying_a_decision_request(self):
        endpoint = Endpoint(lambda _: (503, {"error": "unavailable"}))
        self.addCleanup(endpoint.close)
        with self.assertRaisesRegex(DecisionError, "HTTP 503") as failed:
            Engine(endpoint.url).choose(request_body({}, [Action("wait:1", "Wait", "wait")], "Play"))
        self.assertEqual(len(endpoint.requests), 1)
        self.assertEqual(failed.exception.response, {"error": "unavailable"})

    def test_http_failure_retains_raw_or_bounded_response_evidence(self):
        limit = 1024 * 1024
        for raw in (b"upstream unavailable", b"x" * (limit + 100)):
            with self.subTest(length=len(raw)):
                error = HTTPError("http://localhost/choose", 502, "Bad Gateway", {}, io.BytesIO(raw))
                with mock.patch("urllib.request.urlopen", side_effect=error) as send:
                    with self.assertRaisesRegex(DecisionError, "HTTP 502") as failed:
                        Engine(error.url).choose(request_body({}, [Action("wait:1", "Wait", "wait")], "Play"))
                self.assertEqual(failed.exception.response, raw[:limit].decode())
                self.assertEqual("truncated" in str(failed.exception), len(raw) > limit)
                self.assertGreaterEqual(failed.exception.latency, 0)
                self.assertEqual(send.call_count, 1)
                self.assertTrue(error.closed)
