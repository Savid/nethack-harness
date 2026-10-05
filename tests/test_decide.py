from unittest import TestCase
from unittest import mock
import io
import json
from urllib.error import HTTPError
from helpers import Endpoint
from nethack_harness.actions import Action
from nethack_harness.decide import Engine, DecisionError, request_body


class EngineTest(TestCase):
    def test_argument_facts_preserve_choices_modifiers_and_target_evidence(self):
        adjacent = {"direction": "u", "position": [8, 72], "glyph": "#", "colour": "gray", "remembered_terrain": "#"}
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
        self.assertEqual(facts["move:u"]["target_observation"],
                         {"glyph": "#", "colour": "gray", "remembered_terrain": "#"})
        self.assertNotIn("modifier", facts["move:u"])
        self.assertEqual(facts["move:no_pickup:u"]["modifier"]["name"], "no_pickup")
        lava = dict(adjacent, glyph="}", colour="red", remembered_terrain="}", remembered_feature="lava")
        withheld = Action("move:no_pickup:u", "Move northeast", "move", "u", target=(7, 71), withheld="toward lava")
        liquid = request_body(dict(observation, adjacent=[lava]), [withheld], "Go", tool="move")["state"]["argument_facts"]
        self.assertEqual(liquid["move:no_pickup:u"]["target_observation"]["remembered_feature"], "lava")
        self.assertEqual(liquid["move:no_pickup:u"]["modifier"]["withheld"], "toward lava")
        self.assertEqual(facts["pause"]["max_steps"], 0)
        # Vertical and self directions address the same coordinates but differ mechanically.
        actions = [Action("open:" + key, "Open", "open", target=(8, 70)) for key in ".<>"]
        facts = request_body(observation, actions, "Open", tool="open")["state"]["argument_facts"]
        self.assertEqual([facts[a.id]["direction"] for a in actions], ["self", "up", "down"])
        self.assertTrue(all(f["delta"] == [0, 0] for f in facts.values()))
        self.assertTrue(all("target_observation" not in f for f in facts.values()))

    def test_destinations_are_visible_before_selecting_a_navigation_tool(self):
        route = ((2, 4), (2, 5), (2, 6))
        action = Action("travel:3,7", "Travel to down stairs", "travel", steps=2, target=(2, 6), route=route,
                        subject="down stairs")
        observation = {"phase": "play", "level": {"id": "level-1", "known_terrain": ["..."], "visits": [[3, 7, 2]]}}
        request = request_body(observation, [action], "Explore")
        self.assertEqual(request["state"]["decision"]["stage"], "tool")
        self.assertEqual(request["state"]["navigation"]["destinations"], [
            {"action": "travel:3,7", "kind": "down stairs", "position": [3, 7], "path_length": 3,
             "next_position": [3, 5], "max_steps": 2, "visits": 2, "frontier": False}])
        arguments = request_body(observation, [action], "Explore", tool="travel")["state"]
        self.assertNotIn("navigation", arguments)
        self.assertEqual(arguments["argument_facts"]["travel:3,7"]["visits"], 2)

    def test_requests_carry_recent_attempts_without_screen_hashes_or_repeated_descriptions(self):
        point = {"fingerprint": "f" * 64, "phase": "play", "position": [3, 4], "turn": 9, "level": "level-1"}
        attempts = [{"attempt": n, "decision": 100 + n, "source": "engine", "before": point, "after": point,
                     "action": {"id": "move:l", "description": "Move east " * 20, "kind": "move"},
                     "inputs": [{"keys": "l", "source": "action", "status": "completed"}],
                     "result": {"reason": "no_observed_effect"}} for n in range(1, 21)]
        movements = [{"decision": n, "action": "travel:3,9", "position_before": [3, 4], "position_after": [3, 9]}
                     for n in range(20)]
        observation = {"phase": "play", "objective_progress": {"id": "scope", "start": point, "scope_attempts": 30,
                                                               "recent_attempts": attempts, "omitted_attempts": 10},
                       "navigation_progress": {"recent_positions": [[3, 4]], "recent_actions": movements,
                                               "window_actions": 32}}
        sent = request_body(observation, [Action("wait:1", "Wait", "wait")], "Wait")["state"]["observation"]
        progress, navigation = sent["objective_progress"], sent["navigation_progress"]
        self.assertEqual([a["attempt"] for a in progress["recent_attempts"]], list(range(13, 21)))
        self.assertEqual(progress["omitted_attempts"], 22)
        self.assertEqual(progress["recent_attempts"][-1]["action"], "move:l")
        self.assertEqual(progress["recent_attempts"][-1]["after"]["position"], [3, 4])
        self.assertNotIn("fingerprint", json.dumps(sent))
        self.assertNotIn("decision", json.dumps(sent))
        self.assertEqual([a["action"] for a in navigation["recent_actions"]], ["travel:3,9"] * 8)
        self.assertEqual(len(observation["objective_progress"]["recent_attempts"]), 20)

    def test_requests_omit_map_memory_the_screen_shows_but_keep_level_facts(self):
        observation = {"phase": "play", "fingerprint": "f", "messages": [{"text": str(n)} for n in range(20)],
                       "inventory": {"items": {}, "observed_turn": None, "age_turns": None, "complete": False},
                       "spells": {"items": {"a": "force bolt"}, "observed_turn": 3, "age_turns": 1, "complete": True},
                       "level": {"id": "level-2", "label": "Dlvl:2", "known_terrain": ["#"], "visits": [[3, 4, 1]],
                                 "inferred_floor": [], "searches": [[3, 4, 5]], "inspections": []},
                       "map": ["@"]}
        sent = request_body(observation, [Action("wait:1", "Wait", "wait")], "Wait")["state"]["observation"]
        self.assertEqual(sent["level"], {"id": "level-2", "label": "Dlvl:2", "searches": [[3, 4, 5]]})
        self.assertEqual([m["text"] for m in sent["messages"]], [str(n) for n in range(12, 20)])
        self.assertNotIn("inventory", sent)
        self.assertEqual(sent["spells"], {"items": {"a": "force bolt"}, "age_turns": 1, "complete": True})
        self.assertNotIn("fingerprint", sent)
        self.assertEqual(sent["map"], ["@"])

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
