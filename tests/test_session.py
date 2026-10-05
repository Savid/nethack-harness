from unittest import TestCase
import tempfile
from dataclasses import replace

from helpers import Endpoint, FakeTerm, screen, view
from nethack_harness.actions import Action
from nethack_harness.decide import Engine
from nethack_harness.session import Session
from nethack_harness.settings import Settings
from nethack_harness.store import Store
from nethack_harness.transport import Held


class SessionTest(TestCase):
    @staticmethod
    def navigation_frames(columns, discovery_at=None):
        frames = []
        for index, column in enumerate(columns):
            row = list(" |.......| ")
            row[column] = "@"
            frames.append(view(screen("", [" --------- ", "".join(row), " --------- ",
                                          "   #" if discovery_at is not None and index >= discovery_at else ""],
                                     status2="Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:%d" % (400 + index)),
                               (2, column)))
        return frames

    @staticmethod
    def alternating_moves(request):
        state = request["state"]
        choice = "move" if state["decision"]["stage"] == "tool" else (
            "move:l" if state["observation"]["hero"]["position"][1] == 4 else "move:h")
        return {"answers": {"action": {"choice": choice}}}

    def session(self, response, frames=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = Store(directory.name)
        self.addCleanup(store.close)
        endpoint = Endpoint(response)
        self.addCleanup(endpoint.close)
        return Session(FakeTerm(frames), Engine(endpoint.url), store, Settings()), endpoint

    def test_repeated_navigation_cycle_returns_evidence_to_caller(self):
        session, endpoint = self.session(self.alternating_moves, self.navigation_frames([3, 4, 3, 4, 3]))
        for _ in range(7):
            self.assertIsNone(session.step())
        self.assertEqual(session.step(), "navigation_cycle")
        self.assertEqual(session.term.sent, ["l", "h", "l", "h"])
        record = list(session.store.records())[-1]
        review = record["outcome"]["review"]
        self.assertEqual(review["cycle"], [[3, 4], [3, 5], [3, 4]])
        self.assertEqual(len(review["actions"]), 4)
        self.assertEqual(record["outcome"]["execution_reason"], "completed")
        for request in endpoint.requests[2:]:
            self.assertTrue(request["state"]["observation"]["navigation_progress"]["recent_actions"])
        session.resume()
        self.assertEqual(session.observe()["navigation_progress"]["recent_actions"], [])

    def test_new_terrain_and_changed_objectives_reset_navigation_cycle_evidence(self):
        for reset in ("terrain", "objective"):
            with self.subTest(reset=reset):
                session, _ = self.session(self.alternating_moves, self.navigation_frames(
                    [3, 4, 3, 4, 3], discovery_at=2 if reset == "terrain" else None))
                for index in range(8):
                    if reset == "objective" and index == 4:
                        session.settings = replace(session.settings, objective="Return to the previous square")
                    self.assertIsNone(session.step())

    def test_argument_stage_can_handoff_uncertainty_without_game_input(self):
        session, endpoint = self.session(lambda request: {"answers": {"action": {"choice":
                                        "move" if request["state"]["decision"]["stage"] == "tool" else "pause"}}})
        self.assertIsNone(session.step())
        self.assertEqual(session.step(), "requested_pause")
        self.assertEqual(session.term.sent, [])
        self.assertIn("pause", endpoint.requests[1]["questions"]["action"]["criteria"])
        self.assertEqual(session.last["outcome"]["review"]["decision_stage"], "arguments")
        self.assertEqual(session.last["outcome"]["review"]["selected_tool"], "move")

    def test_one_attempt_budget_stops_even_when_movement_is_blocked(self):
        session, endpoint = self.session(lambda request: {"answers": {"action": {"choice":
                                        "move" if request["state"]["decision"]["stage"] == "tool" else "move:h"}}})
        session.settings = replace(session.settings, max_action_attempts=1)
        self.assertIsNone(session.step())
        self.assertEqual(session.observe()["objective_progress"]["attempts_used"], 0)
        self.assertEqual(session.step(), "action_budget")
        self.assertEqual(session.step(), "action_budget")
        self.assertEqual(len(endpoint.requests), 2)
        self.assertEqual(session.term.sent, ["h"])
        outcome = session.last["outcome"]
        self.assertEqual(outcome["execution_reason"], "no_observed_effect")
        progress = session.observe()["objective_progress"]
        self.assertEqual(progress["attempts_remaining"], 0)
        event, = progress["recent_attempts"]
        self.assertEqual(event["before"]["position"], event["after"]["position"])
        self.assertEqual(event["inputs"], [{"keys": "h", "source": "action", "status": "completed"}])
        self.assertEqual(event["result"]["reason"], "no_observed_effect")

    def test_scoped_progress_reaches_both_stages_and_same_objective_resume_resets_it(self):
        session, endpoint = self.session(self.alternating_moves, self.navigation_frames([3, 4, 3, 4]))
        for _ in range(4):
            self.assertIsNone(session.step())
        first = endpoint.requests[0]["state"]["observation"]["objective_progress"]
        self.assertEqual(first["attempts_used"], 0)
        self.assertEqual(first["recent_attempts"], [])
        for request in endpoint.requests[2:4]:
            progress = request["state"]["observation"]["objective_progress"]
            self.assertEqual(progress["id"], first["id"])
            self.assertEqual(progress["attempts_used"], 1)
            event, = progress["recent_attempts"]
            self.assertEqual(event["action"], "move:l")
            self.assertEqual(event["after"]["position"], [3, 5])
            self.assertEqual(event["after"]["turn"], 401)
        session.resume()
        session.step()
        fresh = endpoint.requests[-1]["state"]["observation"]["objective_progress"]
        self.assertNotEqual(fresh["id"], first["id"])
        self.assertEqual(fresh["attempts_used"], 0)
        self.assertEqual(fresh["recent_attempts"], [])
        self.assertEqual(fresh["start"]["turn"], 402)

    def test_uncertain_first_write_consumes_attempt_and_cannot_be_retried(self):
        session, endpoint = self.session(lambda request: {"answers": {"action": {"choice":
                                        "move" if request["state"]["decision"]["stage"] == "tool" else "move:l"}}})
        session.settings = replace(session.settings, max_action_attempts=1)

        def uncertain_send(keys, before_send=None):
            before_send(session.term.view())
            raise Held("unknown input result")

        session.term.send = uncertain_send
        session.step()
        self.assertEqual(session.step(), "input_held")
        progress = session.observe()["objective_progress"]
        self.assertEqual(progress["attempts_used"], 1)
        self.assertEqual(progress["recent_attempts"][0]["inputs"][0]["status"], "pending")
        self.assertEqual(session.step(), "action_budget")
        self.assertEqual(len(endpoint.requests), 2)

    def test_action_budget_allows_contract_followups_but_not_another_prompt_answer(self):
        frames = [view(), view(screen("In what direction?"), (0, 18)), view()]
        session, _ = self.session(lambda request: {"answers": {"action": {"choice":
                                 "open" if request["state"]["decision"]["stage"] == "tool" else "open:h"}}}, frames)
        session.settings = replace(session.settings, max_action_attempts=1)
        session.step()
        self.assertEqual(session.step(), "action_budget")
        self.assertEqual(session.term.sent, ["o", "h"])
        self.assertEqual(session.observe()["objective_progress"]["attempts_used"], 1)

        session, endpoint = self.session(lambda _: {"answers": {"action": {"choice": "cast"}}},
                                        [view(), view(screen("Cast which spell? [a or ?*]"), (0, 30))])
        session.settings = replace(session.settings, max_action_attempts=1)
        self.assertEqual(session.step(), "action_budget")
        self.assertEqual(session.last["outcome"]["execution_reason"], "prompt")
        self.assertEqual(session.step(), "action_budget")
        self.assertEqual(session.term.sent, ["Z"])
        self.assertEqual(len(endpoint.requests), 1)

    def test_repeat_of_failed_action_hands_back_control_without_another_input(self):
        session, endpoint = self.session(lambda request: {"answers": {"action": {"choice":
                                        "move" if request["state"]["decision"]["stage"] == "tool" else "move:h"}}})
        session.step()
        session.step()
        session.step()
        self.assertEqual(session.step(), "repeated_no_effect")
        self.assertEqual(session.term.sent, ["h"])
        records = list(session.store.records())
        failed, repeated = records[1], records[3]
        self.assertEqual(failed["outcome"]["reason"], "no_observed_effect")
        self.assertEqual(repeated["inputs"], [])
        self.assertEqual(repeated["outcome"]["review"]["previous_decision"], failed["id"])
        self.assertEqual(repeated["response"], {"answers": {"action": {"choice": "move:h"}}})
        self.assertEqual(len(endpoint.requests), 4)
        session.resume()
        session.step()
        session.step()
        self.assertEqual(session.term.sent, ["h", "h"])

    def test_unchanged_travel_and_exploration_handoff_before_duplicate_input(self):
        for kind in ("travel", "explore"):
            with self.subTest(kind=kind):
                initial = view(screen("", ["  #@##"]), (1, 3))
                session, endpoint = self.session(lambda request: {"answers": {"action": {"choice":
                    kind if request["state"]["decision"]["stage"] == "tool" else kind + ":2,6"}}}, [initial])
                for _ in range(3):
                    self.assertIsNone(session.step())
                self.assertEqual(session.step(), "repeated_no_effect")
                self.assertEqual(session.term.sent, ["ml"])
                failed, repeated = list(session.store.records())[1::2]
                self.assertEqual(failed["outcome"]["reason"], "no_observed_effect")
                self.assertEqual(repeated["inputs"], [])
                self.assertEqual(repeated["outcome"]["review"]["previous_decision"], failed["id"])
                self.assertEqual(len(endpoint.requests), 4)

    def test_call_budget_cannot_be_exceeded_after_an_interrupted_selection(self):
        session, endpoint = self.session(lambda _: {"answers": {"action": {"choice": "move"}}})
        session.settings = replace(session.settings, review_after_calls=1)
        session.step(cancelled=lambda: True)
        self.assertEqual(session.step(), "review_budget")
        self.assertEqual(len(endpoint.requests), 1)
        self.assertEqual(session.review_calls, 1)
        self.assertEqual(session.term.sent, [])
        self.assertEqual(list(session.store.records())[0]["outcome"]["reason"], "caller_interrupt")

    def test_turn_consuming_stationary_actions_do_not_trigger_no_effect_handoff(self):
        frames = [view(screen("", [" ---- ", " |.@.| ", " ---- "],
                              status2="Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:%d" % t)) for t in (400, 401, 402)]
        session, _ = self.session(lambda request: {"answers": {"action": {"choice":
                                 "search" if request["state"]["decision"]["stage"] == "tool" else "search:1"}}}, frames)
        for _ in range(4):
            self.assertIsNone(session.step())
        self.assertEqual(session.term.sent, ["s", "s"])
        self.assertEqual(session.turns, 2)

    def test_refused_wait_hands_back_control_instead_of_repeating_a_no_op(self):
        session, _ = self.session(lambda request: {"answers": {"action": {"choice":
                                 "wait" if request["state"]["decision"]["stage"] == "tool" else "wait:8"}}})
        for _ in range(3):
            self.assertIsNone(session.step())
        self.assertEqual(session.step(), "repeated_no_effect")
        self.assertEqual(session.term.sent, ["."])

    def test_changed_screen_allows_retry_of_failed_action(self):
        session, _ = self.session(lambda request: {"answers": {"action": {"choice":
                                 "move" if request["state"]["decision"]["stage"] == "tool" else "move:h"}}})
        session.step()
        session.step()
        session.term.views = [view(screen("Something changed.", [" ---- ", " |.@.| ", " ---- "]))]
        session.step()
        self.assertIsNone(session.step())
        self.assertEqual(session.term.sent, ["h", "h"])

    def test_unexpected_followup_prompt_returns_control(self):
        session, _ = self.session(lambda request: {"answers": {"action": {"choice":
                                 "open" if request["state"]["decision"]["stage"] == "tool" else "open:h"}}})
        session.step()
        self.assertEqual(session.step(), "unexpected_prompt")
        self.assertEqual(session.term.sent, ["o"])

    def test_engine_owns_action_and_transition_records_actual_result(self):
        before = view(screen("", [" ------ ", " |.@e.| ", " ------ "],
                             status2="Dlvl:3 $:0 HP:1(16) Pw:2(2) AC:6 Xp:2 T:400"))
        after = view(screen("Do you want your possessions identified? [ynq] (n)"), (0, 10))
        session, endpoint = self.session(lambda _: {"answers": {"action": {"choice": "attack" if _["state"]["decision"]["stage"] == "tool" else "attack:l", "confidence": 0.01}}},
                                         [before, after])
        self.assertIsNone(session.step())
        self.assertEqual(session.term.sent, [])
        self.assertEqual(session.step(), "game_over")
        self.assertEqual(session.term.sent, ["Fl"])
        selection, record = session.store.records()
        self.assertEqual(selection["choice"], "attack")
        self.assertEqual(selection["outcome"]["reason"], "tool_selected")
        self.assertEqual(record["source"], "engine")
        self.assertEqual(record["choice"], "attack:l")
        self.assertTrue(record["outcome"]["terminal"])
        self.assertEqual(record["after"]["phase"], "ended")
        self.assertEqual(record["inputs"], [{"keys": "Fl", "source": "action", "status": "completed"}])
        self.assertEqual(record["request"], endpoint.requests[1])

    def test_endpoint_error_pauses_without_input(self):
        error_body = {"error": "model queue full", "request_id": "abc"}
        session, endpoint = self.session(lambda _: (503, error_body))
        self.assertEqual(session.step(), "decision_error")
        self.assertEqual(session.term.sent, [])
        record, = session.store.records()
        self.assertEqual(record["outcome"]["reason"], "decision_error")
        self.assertEqual(record["inputs"], [])
        self.assertEqual(record["response"], error_body)
        self.assertEqual(len(endpoint.requests), 1)
        self.assertGreater(record["latency"], 0)

    def test_invalid_response_is_recorded_without_sending_input(self):
        response = {"answers": {"action": {"choice": "unavailable"}}}
        session, _ = self.session(lambda _: response)
        self.assertEqual(session.step(), "decision_error")
        record, = session.store.records()
        self.assertEqual(record["response"], response)
        self.assertIsNone(record["choice"])
        self.assertEqual(record["inputs"], [])

    def test_nonfinite_response_metadata_is_rejected_and_recorded(self):
        session, _ = self.session(lambda _: {"answers": {"action": {"choice": "search:1"}}, "confidence": float("nan")})
        self.assertEqual(session.step(), "decision_error")
        record, = session.store.records()
        self.assertIn("NaN", record["response"])
        self.assertEqual(record["inputs"], [])

    def test_manual_input_is_separately_attributed(self):
        session, endpoint = self.session(lambda _: {})
        session.step(manual=Action("manual", "Caller input", "manual", "s"))
        self.assertEqual(endpoint.requests, [])
        record, = session.store.records()
        self.assertEqual(record["source"], "manual")
        self.assertEqual(record["choice"], "manual")
        self.assertEqual(record["inputs"][0]["keys"], "s")

    def test_caller_input_is_not_refused_by_the_engine_attempt_budget(self):
        session, endpoint = self.session(lambda request: {"answers": {"action": {"choice":
                                        "search" if request["state"]["decision"]["stage"] == "tool" else "search:1"}}})
        session.settings = replace(session.settings, max_action_attempts=1)
        self.assertEqual(session.step(), None)
        self.assertEqual(session.step(), "action_budget")
        session.step(manual=Action("manual", "Caller input", "manual", "\x04l"))
        self.assertEqual(session.term.sent, ["s", "\x04l"])
        self.assertEqual(session.last["source"], "manual")
        self.assertEqual(session.step(), "action_budget")
        self.assertEqual(len(endpoint.requests), 2)

    def test_manual_catalogue_action_is_revalidated_after_polling(self):
        session, endpoint = self.session(lambda _: {})
        session.observe()
        action = next(a for a in session.offered() if a.id == "move:l")
        session.term.on_poll = lambda: setattr(session.term, "views", [view(screen("In what direction?"), (0, 18))])
        session.step(manual=action)
        record, = session.store.records()
        self.assertEqual(record["outcome"]["reason"], "observation_changed")
        self.assertEqual(record["outcome"]["action"]["id"], "move:l")
        self.assertEqual(record["inputs"], [])
        self.assertEqual(session.term.sent, [])
        self.assertEqual(endpoint.requests, [])
        self.assertEqual(session.observe()["objective_progress"]["attempts_used"], 0)

    def test_partial_execution_failure_preserves_action_frames_and_counters(self):
        frames = [view(screen("", [" ---- ", " |.@.| ", " ---- "],
                              status2="Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:%d" % t)) for t in (400, 401)]
        session, _ = self.session(lambda request: {"answers": {"action": {"choice":
                                  "wait" if request["state"]["decision"]["stage"] == "tool" else "wait:8"}}}, frames)
        original_send = session.term.send

        def fail_second_input(keys, before_send=None):
            if session.term.sent:
                before_send(session.term.view())
                raise Held("waiting")
            return original_send(keys, before_send=before_send)

        session.term.send = fail_second_input
        session.step()
        self.assertEqual(session.step(), "input_held")
        _, record = session.store.records()
        self.assertEqual(record["outcome"]["action"]["id"], "wait:8")
        self.assertEqual(record["outcome"]["steps"], 1)
        self.assertEqual(record["outcome"]["elapsed_turns"], 1)
        self.assertEqual(len(record["outcome"]["frames"]), 1)
        self.assertEqual([item["status"] for item in record["inputs"]], ["completed", "pending"])
        self.assertEqual((session.turns, session.actions), (1, 1))

    def test_tool_and_arguments_execute_one_bounded_action(self):
        frames = [view(screen("", [" ---- ", " |.@.| ", " ---- "],
                              status2="Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:%d" % t)) for t in range(400, 409)]
        session, endpoint = self.session(lambda _: {"answers": {"action": {"choice": "search" if _["state"]["decision"]["stage"] == "tool" else "search:8"}}}, frames)
        session.settings = replace(session.settings, max_action_attempts=1)
        self.assertIsNone(session.step())
        self.assertEqual(session.term.sent, [])
        self.assertEqual(session.step(), "action_budget")
        self.assertEqual(len(endpoint.requests), 2)
        self.assertEqual(session.calls, 2)
        self.assertEqual(session.actions, 1)
        self.assertEqual(session.term.sent, ["s"] * 8)
        self.assertEqual(session.turns, 8)
        self.assertEqual(session.observe()["objective_progress"]["attempts_used"], 1)

    def test_grouped_command_selects_and_executes_its_concrete_command(self):
        session, endpoint = self.session(lambda request: {"answers": {"action": {"choice":
                                        "command" if request["state"]["decision"]["stage"] == "tool" else "pay"}}})
        self.assertIsNone(session.step())
        self.assertEqual(session.term.sent, [])
        offered = {a.id for a in session.offered() if a.tool == "command"}
        self.assertIsNone(session.step())
        self.assertEqual(session.term.sent, ["#pay\r"])
        self.assertEqual(set(endpoint.requests[1]["questions"]["action"]["criteria"]), offered | {"pause"})
        _, record = session.store.records()
        self.assertEqual(record["outcome"]["action"]["kind"], "pay")
        self.assertEqual(record["outcome"]["action"]["tool"], "command")

    def test_pagination_is_recorded_without_a_decision_request(self):
        session, endpoint = self.session(lambda _: {}, [view(screen("Hello.--More--"), (0, 20)), view()])
        session.step()
        self.assertEqual(endpoint.requests, [])
        record, = session.store.records()
        self.assertEqual(record["source"], "protocol")
        self.assertEqual(session.term.sent, [" "])
        self.assertEqual(session.observe()["objective_progress"]["attempts_used"], 0)

    def test_argument_failure_records_both_calls_without_input(self):
        session, endpoint = self.session(lambda request: {"answers": {"action": {"choice": "move"}}}
                                        if request["state"]["decision"]["stage"] == "tool" else (503, {}))
        self.assertIsNone(session.step())
        self.assertEqual(session.step(), "decision_error")
        self.assertEqual(session.calls, 2)
        self.assertEqual(session.term.sent, [])
        selected, failed = session.store.records()
        self.assertEqual(selected["choice"], "move")
        self.assertEqual(failed["request"], endpoint.requests[1])
        self.assertEqual(failed["request"]["state"]["decision"]["tool"], "move")
        self.assertTrue(all(k.startswith("move:") or k == "pause"
                            for k in endpoint.requests[1]["questions"]["action"]["criteria"]))

    def test_tool_selection_is_discarded_when_context_changes(self):
        for changed in ("screen", "objective", "context", "interruption"):
            with self.subTest(changed=changed):
                session, endpoint = self.session(lambda _: {"answers": {"action": {"choice": "move"}}})
                session.step(cancelled=lambda: changed == "interruption")
                if changed == "screen":
                    session.term.views = [view(screen("A new message.", [" ---- ", " |.@.| ", " ---- "]))]
                elif changed == "objective":
                    session.settings = replace(session.settings, objective="Return upstairs")
                elif changed == "context":
                    session.settings = replace(session.settings, caller_context={"caller": "changed goal"})
                session.step()
                self.assertEqual([r["state"]["decision"]["stage"] for r in endpoint.requests], ["tool", "tool"])
                self.assertEqual(session.term.sent, [])

    def test_prompt_uses_one_direct_choice(self):
        session, endpoint = self.session(lambda _: {"answers": {"action": {"choice": "input:6c"}}},
                                        [view(screen("In what direction?"), (0, 18)), view()])
        session.step()
        self.assertEqual(session.term.sent, ["l"])
        self.assertEqual(endpoint.requests[0]["state"]["decision"]["stage"], "input")
        self.assertEqual(session.calls, 1)
