from unittest import TestCase
from unittest import mock
import io
import json
import socket
from urllib.error import HTTPError
from helpers import Endpoint
from nethack_harness.actions import Action
from nethack_harness import decide
from nethack_harness.decide import Engine, DecisionError, estimated_tokens, request_body
from nethack_harness.tools import TOOLS


class EngineTest(TestCase):
    def test_argument_requests_offer_the_tool_and_keep_square_facts_in_the_observation(self):
        adjacent = {"direction": "u", "position": [71, 7], "glyph": "#", "colour": "default", "remembered_terrain": "#"}
        observation = {"phase": "play", "hero": {"position": [70, 8]}, "adjacent": [adjacent]}
        actions = [Action("move:u", "Move northeast", "move", "u", target=(7, 71)),
                   Action("move:no_pickup:u", "Move northeast without pickup", "move", "mu", target=(7, 71)),
                   Action("open:l", "Open east", "open", "o", target=(8, 71)),
                   Action("pause", "Return control", "pause", steps=0)]
        request = request_body(observation, actions, "Reach 71,7", tool="move")
        self.assertEqual(request["state"]["decision"], {"stage": "arguments", "tool": "move"})
        self.assertEqual(set(request["questions"]["action"]["criteria"]), {"move:u", "move:no_pickup:u", "pause"})
        square = request["state"]["observation"]["adjacent"]["u"]
        self.assertIn("71,7", square)
        self.assertIn("'#'", square)
        lava = dict(adjacent, glyph="}", colour="red", remembered_terrain="}", remembered_feature="lava")
        withheld = Action("move:no_pickup:u", "Move northeast", "move", "u", target=(7, 71),
                          withheld="modifier withheld toward remembered lava")
        liquid = request_body(dict(observation, adjacent=[lava]), [withheld], "Go", tool="move")
        self.assertIn("lava", liquid["state"]["observation"]["adjacent"]["u"])
        self.assertIn("withheld", liquid["questions"]["action"]["criteria"]["move:no_pickup:u"])

    def test_argument_choices_keep_their_modifier_and_say_here_up_or_down(self):
        adjacent = {"direction": "u", "position": [71, 7], "glyph": "d", "colour": "yellow", "remembered_terrain": "."}
        observation = {"phase": "play", "hero": {"position": [70, 8]}, "adjacent": [adjacent]}
        actions = [Action("move:u", "Move", "move", "u", target=(7, 71)),
                   Action("move:no_pickup:u", "Move", "move", "mu", target=(7, 71))]
        actions += [Action("open:" + key, "Open", "open", "o", target=(8, 70)) for key in "u.<>"]
        actions.append(Action("pause", "Return control to the caller", "pause", steps=0))
        questions = request_body(observation, actions, "Go")["questions"]
        # The plain move's square is described in observation.adjacent; what the modifier does is not.
        self.assertEqual(questions["move"]["criteria"]["move:u"], "")
        no_pickup = next(v for v in TOOLS["move"].variants if v.name == "no_pickup")
        self.assertEqual(questions["move"]["criteria"]["move:no_pickup:u"], no_pickup.description)
        vertical = [questions["open"]["criteria"]["open:" + key] for key in ".<>"]
        self.assertTrue(all(vertical))
        self.assertEqual(len(set(vertical)), 3)
        engulfed = request_body(dict(observation, adjacent=[]), actions, "Go", tool="open")["questions"]["action"]
        self.assertTrue(engulfed["criteria"]["open:u"])

    def test_each_destination_is_described_once_under_its_action_id(self):
        route = ((2, 4), (2, 5), (2, 6))
        travel = Action("travel:6,2", "Travel to down stairs", "travel", steps=2, target=(2, 6), route=route,
                        subject="down stairs")
        explore = Action("explore:9,4", "Explore", "explore", steps=8, target=(4, 9), route=route,
                         subject="corridor endpoint", frontier=True)
        actions = [travel, explore, Action("pause", "Return control", "pause", steps=0)]
        observation = {"phase": "play", "level": {"id": "level-1", "known_terrain": ["..."], "visits": [[6, 2, 2]]}}
        request = request_body(observation, actions, "Explore")
        destinations = request["state"]["navigation"]["destinations"]
        self.assertEqual(set(destinations), {"travel:6,2", "explore:9,4"})
        self.assertIn("down stairs", destinations["travel:6,2"])
        self.assertIn("frontier", destinations["explore:9,4"])
        self.assertNotIn("frontier", destinations["travel:6,2"])
        text = json.dumps(request)
        self.assertEqual(text.count("down stairs"), 1)
        arguments = request_body(observation, actions, "Explore", tool="travel")["state"]
        self.assertEqual(set(arguments["navigation"]["destinations"]), {"travel:6,2"})

    def test_one_request_asks_the_tool_and_the_arguments_of_each_tool_with_several(self):
        actions = [Action("move:l", "Move east", "move", "l", target=(2, 4)),
                   Action("move:h", "Move west", "move", "h", target=(2, 2)),
                   Action("pray", "Pray", "pray", "#pray\r"),
                   Action("travel:5,2", "Travel to an endpoint", "travel", target=(2, 5), route=((2, 4), (2, 5)),
                          subject="corridor endpoint"),
                   Action("travel:9,2", "Travel to stairs", "travel", target=(2, 9), route=((2, 4),) * 6,
                          subject="down stairs"),
                   Action("adjust", "Adjust", "adjust", "#adjust\r"), Action("pay", "Pay", "pay", "#pay\r"),
                   Action("pause", "Return control to the caller", "pause", steps=0)]
        request = request_body({"phase": "play"}, actions, "Explore")
        questions = request["questions"]
        # The named commands are asked only once the command tool has been chosen.
        self.assertEqual(list(questions), ["action", "move", "travel"])
        self.assertEqual(set(questions["action"]["criteria"]), {"move", "pray", "travel", "command", "pause"})
        self.assertEqual(set(questions["move"]["criteria"]), {"move:l", "move:h", "pause"})
        self.assertEqual(set(questions["travel"]["criteria"]), {"travel:5,2", "travel:9,2", "pause"})
        answers = {"action": {"choice": "travel"}, "travel": {"choice": "travel:9,2"}, "move": {"choice": "west"}}
        for reply, chosen in (({"answers": answers}, {"action": "travel", "travel": "travel:9,2"}),
                              ({"answers": {"action": {"choice": "pray"}}}, {"action": "pray"}),
                              ({"answers": {"action": {"choice": "command"}}}, {"action": "command"}),
                              ({"answers": {"action": {"choice": "travel"}}}, "answers.travel.choice"),
                              ({"answers": dict(answers, travel={"choice": "move:l"})}, "answers.travel.choice")):
            with self.subTest(reply=reply):
                endpoint = Endpoint(lambda _, reply=reply: reply)
                try:
                    if isinstance(chosen, dict):
                        self.assertEqual(Engine(endpoint.url).choose(request)[0], chosen)
                    else:
                        with self.assertRaisesRegex(DecisionError, chosen.replace(".", r"\.")):
                            Engine(endpoint.url).choose(request)
                finally:
                    endpoint.close()
        commands = request_body({"phase": "play"}, actions, "Explore", tool="command")["questions"]["action"]
        self.assertEqual(set(commands["criteria"]), {"adjust", "pay", "pause"})

    def test_argument_questions_take_only_the_room_the_budget_leaves(self):
        actions = [Action("move:" + key, "Move " + key, "move", key, target=(2, 4)) for key in "hjkl"]
        actions += [Action("search:%d" % n, "Search %d times" % n, "search", "s", n) for n in (1, 8)]
        actions.append(Action("pause", "Return control to the caller", "pause", steps=0))
        whole = request_body({"phase": "play", "messages": []}, actions, "Explore")
        self.assertEqual(list(whole["questions"]), ["action", "move", "search"])
        budget = estimated_tokens(dict(whole, questions={key: whole["questions"][key] for key in ("action", "move")}))
        trimmed = request_body({"phase": "play", "messages": []}, actions, "Explore", max_tokens=budget)
        self.assertEqual(list(trimmed["questions"]), ["action", "move"])
        self.assertNotIn("omitted", trimmed["state"])

    @staticmethod
    def attempts(count, keys, action=None):
        point = {"fingerprint": "f" * 64, "phase": "play", "position": [3, 2], "turn": 9, "level": "level-1"}
        moved = dict(point, position=[4, 2], turn=10)
        action = action or {"id": "travel:8,2", "description": "Move toward a frontier", "kind": "travel"}
        return [{"attempt": n, "decision": 100 + n, "source": "engine", "before": point, "after": moved,
                 "action": action, "inputs": [{"keys": "ml", "source": "action", "status": "completed"}] * keys,
                 "result": {"reason": "completed", "steps": 2, "elapsed_turns": 1}} for n in range(1, count + 1)]

    def test_requests_carry_the_latest_attempts_however_many_keys_they_sent(self):
        def sent(keys):
            observation = {"phase": "play", "objective_progress": {
                "id": "scope", "scope_attempts": 30, "recent_attempts": self.attempts(20, keys),
                "attempts_remaining": None}}
            return request_body(observation, [Action("wait:1", "Wait", "wait")], "Wait")
        one, many = sent(1), sent(32)
        history = one["state"]["observation"]["history"]
        self.assertEqual(history["attempts"], 30)
        self.assertEqual(len(history["recent"]), 8)
        self.assertTrue(all(line.startswith("travel:8,2") and "3,2 to 4,2" in line for line in history["recent"]))
        self.assertNotIn("ffff", json.dumps(one))
        # An attempt's keys stay in the decision log; the request's size does not grow with them.
        self.assertEqual(len(json.dumps(one)), len(json.dumps(many)))
        digit = {"id": "input:31", "description": "Type quantity digit 1", "kind": "input"}
        observation = {"phase": "item", "objective_progress": {"scope_attempts": 1, "recent_attempts": self.attempts(
            1, 1, digit), "attempts_remaining": None}}
        line, = request_body(observation, [Action("input:31", "Type 1", "input", "1")], "Drop")["state"][
            "observation"]["history"]["recent"]
        self.assertIn("Type quantity digit 1", line)

    def test_requests_omit_map_memory_the_screen_shows_but_keep_level_facts(self):
        observation = {"phase": "play", "fingerprint": "f", "messages": [{"text": str(n)} for n in range(20)],
                       "inventory": {"items": {}, "observed_turn": None, "age_turns": None, "complete": False},
                       "spells": {"items": {"a": "force bolt"}, "observed_turn": 3, "age_turns": 1, "complete": True},
                       "level": {"id": "level-2", "label": "Dlvl:2", "known_terrain": ["#"], "visits": [[3, 2, 1]],
                                 "inferred_floor": [], "searches": [[3, 2, 5]], "inspections": []},
                       "map": ["@"]}
        sent = request_body(observation, [Action("wait:1", "Wait", "wait")], "Wait")["state"]["observation"]
        self.assertEqual(sent["level"], {"id": "level-2", "label": "Dlvl:2", "searches": {"3,2": 5}})
        self.assertEqual(sent["messages"], [str(n) for n in range(12, 20)])
        self.assertNotIn("inventory", sent)
        self.assertEqual(sent["spells"], {"items": {"a": "force bolt"}, "age_turns": 1, "complete": True})
        self.assertNotIn("fingerprint", sent)
        self.assertEqual(sent["map"], ["@"])

    def test_a_request_over_its_budget_omits_in_a_fixed_order_and_says_what(self):
        def landmarks(level):
            return [{"position": [x, 5], "kind": "fountain", "glyph": "{", "observed_turn": 3, "source": level}
                    for x in range(1, 40)]
        observation = {"phase": "play", "level": {"id": "level-2", "label": "Dlvl:2"},
                       "messages": [{"turn": n, "text": "Message %d." % n} for n in range(20)],
                       "known_levels": [{"id": "level-1", "label": "Dlvl:1", "landmarks": landmarks("level-1"),
                                         "connections": []},
                                        {"id": "level-2", "label": "Dlvl:2", "landmarks": landmarks("level-2"),
                                         "connections": []}]}
        actions = [Action("travel:%d,1" % length, "Move toward a corridor junction", "travel", steps=8,
                          target=(1, length), route=tuple((1, x) for x in range(1, length + 1)),
                          subject="corridor junction") for length in range(30, 0, -1)]
        actions.append(Action("pause", "Return control", "pause", steps=0))
        # Each budget is one token under the previous request without its argument questions, which only use
        # the room left, so each request omits one thing more.
        budget, expected = None, []
        for omission in (None, "landmarks of other levels", "messages before the latest 2",
                         "6 destinations beyond the 24 nearest"):
            expected += [omission] if omission else []
            body = request_body(observation, actions, "Explore", max_tokens=budget)
            self.assertEqual(body["state"].get("omitted", []), expected)
            self.assertLessEqual(estimated_tokens(body), budget or estimated_tokens(body))
            budget = estimated_tokens(dict(body, questions={"action": body["questions"]["action"]})) - 1
        sent = body["state"]["observation"]
        self.assertEqual([level["id"] for level in sent["known_levels"]], ["level-2"])
        self.assertEqual(len(sent["known_levels"][0]["landmarks"]), 39)
        self.assertEqual(sent["messages"], ["T18: Message 18.", "T19: Message 19."])
        self.assertEqual(set(body["state"]["navigation"]["destinations"]),
                         {"travel:%d,1" % length for length in range(1, 25)})

    def test_exact_choice_is_used_without_confidence_gate(self):
        endpoint = Endpoint(lambda _: {"answers": {"action": {"choice": "attack:l", "confidence": 0.01}}})
        self.addCleanup(endpoint.close)
        request = request_body({"hero": {"hp": 1}}, [Action("attack:l", "Attack east", "attack")], "Reach depth 10", tool="attack")
        chosen, reply, seconds, retries = Engine(endpoint.url).choose(request)
        self.assertEqual(retries, [])
        self.assertEqual(chosen, {"action": "attack:l"})
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

    def test_transient_failures_are_retried_after_retry_after_and_every_attempt_is_kept(self):
        replies = [(429, {"error": "queue full"}, {"Retry-After": "2"}), (503, {"error": "restarting"}),
                   {"answers": {"action": {"choice": "wait:1"}}}]
        endpoint = Endpoint(lambda _: replies.pop(0))
        self.addCleanup(endpoint.close)
        request = request_body({}, [Action("wait:1", "Wait", "wait")], "Play")
        with mock.patch.object(decide.time, "sleep") as sleep:
            chosen, _, _, retries = Engine(endpoint.url, timeout=30).choose(request)
        self.assertEqual(chosen, {"action": "wait:1"})
        self.assertEqual(len(endpoint.requests), 3)
        self.assertEqual([(r["status"], r["retry_after"], r["response"]) for r in retries],
                         [(429, 2.0, {"error": "queue full"}), (503, None, {"error": "restarting"})])
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2.0, 1.0])

    def test_retries_stop_at_the_decision_timeout_and_refused_connections_are_retried(self):
        endpoint = Endpoint(lambda _: (429, {"error": "queue full"}, {"Retry-After": "60"}))
        self.addCleanup(endpoint.close)
        request = request_body({}, [Action("wait:1", "Wait", "wait")], "Play")
        with self.assertRaisesRegex(DecisionError, "HTTP 429") as failed:
            Engine(endpoint.url, timeout=30).choose(request)
        self.assertEqual((len(endpoint.requests), failed.exception.retries), (1, []))
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        url = "http://127.0.0.1:%d/choose" % closed.getsockname()[1]
        closed.close()
        with self.assertRaisesRegex(DecisionError, "ConnectionRefusedError") as failed:
            Engine(url, timeout=1.2).choose(request)
        self.assertEqual([retry["error"] for retry in failed.exception.retries],
                         ["decision request failed (ConnectionRefusedError)"])
        self.assertLess(failed.exception.latency, 1.2)

    def test_other_http_failures_are_reported_without_retrying(self):
        for code in (400, 413, 422, 500):
            with self.subTest(code=code):
                endpoint = Endpoint(lambda _, code=code: (code, {"error": "rejected"}))
                try:
                    with self.assertRaisesRegex(DecisionError, "HTTP %d" % code) as failed:
                        Engine(endpoint.url).choose(request_body({}, [Action("wait:1", "Wait", "wait")], "Play"))
                finally:
                    endpoint.close()
                self.assertEqual(len(endpoint.requests), 1)
                self.assertEqual((failed.exception.response, failed.exception.retries), ({"error": "rejected"}, []))

    def test_http_failure_retains_raw_or_bounded_response_evidence(self):
        limit = 1024 * 1024
        for raw in (b"upstream unavailable", b"x" * (limit + 100)):
            with self.subTest(length=len(raw)):
                error = HTTPError("http://localhost/choose", 500, "Internal Server Error", {}, io.BytesIO(raw))
                with mock.patch("urllib.request.urlopen", side_effect=error) as send:
                    with self.assertRaisesRegex(DecisionError, "HTTP 500") as failed:
                        Engine(error.url).choose(request_body({}, [Action("wait:1", "Wait", "wait")], "Play"))
                self.assertEqual(failed.exception.response, raw[:limit].decode())
                self.assertEqual("truncated" in str(failed.exception), len(raw) > limit)
                self.assertGreaterEqual(failed.exception.latency, 0)
                self.assertEqual(send.call_count, 1)
                self.assertTrue(error.closed)
