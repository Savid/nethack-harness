from unittest import TestCase
from copy import deepcopy

from goal_supervisor.conditions import check
from goal_supervisor.goals import GoalBoard
from goal_supervisor.harness import Rejected
from goal_supervisor.runner import step


def observed(column=4, level="level-1"):
    return {"fingerprint": "screen-%s-%s" % (level, column), "phase": "play",
            "hero": {"position": [3, column], "turn": 10 + column}, "level": {"id": level}}


def equals(path, value):
    return {"path": path, "op": "eq", "value": value}


def destination(identifier="destination", column=5, parent=None, attempts=3):
    return {"id": identifier, "objective": "Reach the caller-selected destination.", "parent_id": parent,
            "success": [equals(["level", "id"], "level-1"), equals(["hero", "position"], [3, column])],
            "limits": {"action_attempts": attempts}}


class GoalTest(TestCase):
    def test_replacement_rejects_non_objects_without_removing_original_goals(self):
        board = GoalBoard()
        board.add(destination())
        board.activate("destination", observed())
        before = board.snapshot()
        for value in (None, 1, [["id", "replacement"], ["objective", "New destination"]]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    board.replace("destination", value)
                self.assertEqual(board.snapshot(), before)

    def test_numeric_conditions_accept_arbitrarily_large_finite_integers(self):
        for number in (10 ** 400, -(10 ** 400)):
            with self.subTest(number=number):
                board = GoalBoard()
                board.add({"id": "large", "objective": "Reach the supplied count.", "success": [
                    {"path": ["count"], "op": "gte", "value": number}]})
                self.assertEqual(board.activate("large", {"count": number - 1})["reason"], "ready")
                self.assertEqual(board.assess({"count": number})["reason"], "completed")

    def test_switch_update_replace_and_remove_preserve_history_and_one_active_leaf(self):
        board = GoalBoard()
        board.add({"id": "parent", "objective": "Inspect the area."})
        board.add(destination("first", parent="parent"))
        board.add(destination("second", column=6, parent="parent"))
        self.assertFalse(board.activate("parent", observed())["eligible"])
        board.activate("first", observed())
        board.activate("second", observed())
        self.assertEqual(board.goal("first")["status"], "suspended")
        self.assertEqual(board.state["active"], "second")
        board.update("parent", {"metadata": {"intent": "Caller revised the plan"}})
        self.assertIsNone(board.state["active"])
        self.assertEqual(board.goal("second")["status"], "suspended")
        board.replace("first", destination("replacement", parent="parent"))
        self.assertEqual(board.goal("first")["status"], "removed")
        board.activate("replacement", observed())
        removed = board.remove("parent")
        self.assertIn("replacement", removed)
        self.assertIsNone(board.state["active"])
        self.assertTrue(all(goal["status"] == "removed" for goal in board.state["goals"].values()))
        self.assertTrue(any(event["kind"] == "replaced" for event in board.state["events"]))
        self.assertTrue(any(event["kind"] == "activated" and event["goal_id"] == "first"
                            for event in board.state["events"]))

    def test_parent_cycles_and_replacement_under_removed_subtree_are_rejected_without_edits(self):
        board = GoalBoard()
        board.add({"id": "parent", "objective": "Parent intent"})
        board.add(destination(parent="parent"))
        before = board.snapshot()
        for operation in (lambda: board.update("parent", {"parent_id": "destination"}),
                          lambda: board.replace("parent", destination("replacement", parent="destination"))):
            with self.assertRaises(ValueError):
                operation()
            self.assertEqual(board.snapshot(), before)

    def test_conditional_activation_is_explicit_and_unknown_parent_validity_hands_back_control(self):
        board = GoalBoard()
        board.add(destination("current"))
        board.activate("current", observed())
        board.add({"id": "conditional", "objective": "Caller conditional task", "activate_when": [
            equals(["hero", "ready"], True)], "valid_while": [equals(["level", "id"], "level-1")]})
        board.add(destination("child", parent="conditional"))
        self.assertIsNone(board.activate("child", observed())["eligible"])
        self.assertEqual(board.state["active"], "current")
        ready = observed()
        ready["hero"]["ready"] = True
        self.assertTrue(board.eligibility("child", ready)["eligible"])
        self.assertEqual(board.state["active"], "current")
        board.activate("child", ready)
        self.assertEqual(board.state["active"], "child")
        unknown = deepcopy(ready)
        del unknown["level"]
        result = board.assess(unknown)
        self.assertEqual(result["reason"], "review")
        self.assertIsNone(result["matches"])
        self.assertEqual(result["condition_goal_id"], "conditional")
        self.assertIsNone(board.state["active"])

    def test_open_prompt_defers_unknown_conditions_but_not_known_ones(self):
        board = GoalBoard()
        goal = destination(attempts=5)
        goal["valid_while"] = [equals(["underfoot", "remembered_terrain"], ".")]
        goal["review_when"] = [{"path": ["hero", "hp"], "op": "lt", "value": 5}]
        board.add(goal)
        play = observed()
        play["underfoot"], play["hero"]["hp"] = {"remembered_terrain": "."}, 9
        board.activate("destination", play)
        prompt = {"fingerprint": "prompt", "phase": "choice", "hero": {"position": None, "turn": 14, "hp": 9},
                  "level": {"id": "level-1"}}
        execution = board.begin(play, 0)["context"]["execution"]["id"]
        self.assertEqual(board.finish(execution, prompt, {"boundary": "action_budget", "attempts": 1})["reason"], "ready")
        execution = board.begin(prompt, 1)["context"]["execution"]["id"]
        hurt = dict(prompt, hero=dict(prompt["hero"], hp=3))
        result = board.finish(execution, hurt, {"boundary": "action_budget", "attempts": 1})
        self.assertEqual(result["condition_group"], "review_when")
        board.activate("destination", play)
        self.assertEqual(board.assess(dict(play, underfoot={}))["reason"], "review")

    def test_goal_identity_and_attempts_survive_windows_budget_review_and_revision(self):
        board = GoalBoard()
        board.add(destination(column=7, attempts=2))
        board.activate("destination", observed())
        identifiers = []
        for index in range(2):
            request = board.begin(observed(4 + index), index * 2)
            context = request["context"]
            self.assertEqual(context["goal"]["id"], "destination")
            self.assertEqual(context["goal"]["attempts_used"], index)
            identifiers.append(context["execution"]["id"])
            result = board.finish(identifiers[-1], observed(5 + index), {"boundary": "action_budget", "attempts": 1})
        self.assertNotEqual(*identifiers)
        self.assertEqual(result["boundary"], "goal_budget_exhausted")
        self.assertEqual(board.goal("destination")["status"], "suspended")
        board.update("destination", {"limits": {"action_attempts": 3}})
        self.assertEqual(board.goal("destination")["attempts_used"], 2)
        board.activate("destination", observed(6))
        context = board.begin(observed(6), 4)["context"]
        self.assertEqual(context["goal"]["attempts_remaining"], 1)
        result = board.finish(context["execution"]["id"], observed(7), {"boundary": "action_budget", "attempts": 1})
        self.assertEqual(result["reason"], "completed")
        self.assertEqual(board.goal("destination")["attempts_used"], 3)

    def test_success_needs_level_and_position_and_already_satisfied_goal_sends_no_action(self):
        board = GoalBoard()
        board.add(destination())
        result = board.activate("destination", observed(5, "another-level"))
        self.assertEqual(result["reason"], "ready")
        result = board.assess(observed(5))
        self.assertEqual(result["reason"], "completed")
        self.assertEqual(board.goal("destination")["attempts_used"], 0)
        self.assertEqual(board.state["events"][-1]["source"], "observation_conditions")
        self.assertEqual(board.begin(observed(5), 0)["reason"], "no_active_goal")

    def test_new_window_reports_unfinished_goal_and_prior_actions_without_old_stop_envelopes(self):
        board = GoalBoard()
        board.add(destination(column=9, attempts=8))
        board.activate("destination", observed())
        column = 4
        for target, moved in ((7, True), (7, False), (5, True)):
            context = board.begin(observed(column), 0)["context"]
            if column == 4:
                self.assertEqual(context["history"]["recent_attempts"], [])
            outcome = {"boundary": "action_budget", "execution_reason": "observation_changed", "attempts": 1,
                       "result": {"reason": "action_budget", "review": {"limit_attempts": 1}, "elapsed_turns": 1,
                                  "action": {"id": "travel:3,%d" % target, "target": [3, target]},
                                  "keys": [{"keys": "ml", "source": "action"}], "frames": [{"message": "Observed"}],
                                  "changed_fields": ["entities"]}}
            column += moved
            board.finish(context["execution"]["id"], observed(column), outcome)
        current = board.begin(observed(column), 2)["context"]
        self.assertFalse(current["completion"]["matches"])
        self.assertEqual(current["completion"]["unmet"],
                         [dict(equals(["hero", "position"], [3, 9]), observed=[3, column])])
        self.assertEqual(current["goal"]["attempts_remaining"], 5)
        prior = current["history"]["recent_attempts"]
        self.assertEqual(prior[0], {"action": "travel:3,7", "from": [3, 4], "to": [3, 5],
                                    "result": "observation_changed", "turns": 1, "changed": ["entities"]})
        self.assertEqual(current["history"]["repeated_actions"],
                         [{"action": "travel:3,7", "tries": 2, "moved": 1, "reached_target": 0}])
        self.assertNotIn("frames", board.goal("destination")["recent_windows"][0]["outcome"]["result"])
        self.assertEqual(set(current), {"goal", "completion", "history", "execution"})

        unverified = GoalBoard()
        unverified.add({"id": "inspect", "objective": "Inspect the area."})
        unverified.activate("inspect", observed())
        current = unverified.begin(observed(), 0)["context"]["completion"]
        self.assertEqual(current, {"matches": None, "reason": "caller_confirmation_required"})

    def test_idle_attempts_hand_back_until_the_caller_reactivates(self):
        board = GoalBoard()
        board.add(dict(destination(column=9, attempts=20), limits={"action_attempts": 20, "idle_attempts": 2}))
        board.activate("destination", observed())

        def window(after):
            context = board.begin(observed(), 0)["context"]
            return board.finish(context["execution"]["id"], after,
                                {"boundary": "action_budget", "attempts": 1, "result": {"action": {"id": "move:k"}}})

        self.assertEqual(window(observed())["reason"], "ready")
        self.assertEqual(window(dict(observed(), fingerprint="message changed"))["boundary"], "idle_attempts")
        self.assertEqual(board.goal("destination")["status"], "suspended")
        self.assertEqual(board.activate("destination", observed())["reason"], "ready")
        waited = observed()
        waited["hero"] = dict(waited["hero"], turn=waited["hero"]["turn"] + 1)
        self.assertEqual(window(waited)["reason"], "ready")
        self.assertEqual(board.begin(observed(), 0)["context"]["history"]["idle_streak"], 0)

    def test_latest_names_the_goal_most_recently_worked(self):
        board = GoalBoard()
        self.assertIsNone(board.latest())
        board.add(destination("first", column=9))
        board.add(destination("second", column=9))
        board.activate("first", observed())
        board.add(destination("third", column=9))
        self.assertEqual(board.latest()["id"], "first")
        board.activate("second", observed())
        self.assertEqual(board.latest()["id"], "second")

    def test_caller_completion_is_attributed_and_never_inferred_from_missing_predicates(self):
        board = GoalBoard()
        board.add({"id": "inspect", "objective": "Inspect everything relevant to the caller."})
        self.assertEqual(board.activate("inspect", observed())["reason"], "ready")
        with self.assertRaises(ValueError):
            board.complete("inspect", observed(), "")
        board.complete("inspect", observed(), "The caller reviewed the recorded inspection results.")
        self.assertEqual(board.goal("inspect")["status"], "completed")
        self.assertEqual(board.state["events"][-1]["source"], "caller_confirmation")

    def test_review_conditions_precede_success_and_completion_does_not_complete_parent(self):
        board = GoalBoard()
        board.add({"id": "parent", "objective": "Caller plan", "review_when": [equals(["hero", "changed"], True)]})
        board.add(destination(parent="parent"))
        current = observed()
        current["hero"]["changed"] = False
        board.activate("destination", current)
        arrived = observed(5)
        arrived["hero"]["changed"] = True
        self.assertEqual(board.assess(arrived)["reason"], "review")
        self.assertEqual(board.goal("destination")["status"], "suspended")
        arrived["hero"]["changed"] = False
        self.assertEqual(board.activate("destination", arrived)["reason"], "completed")
        self.assertNotEqual(board.goal("parent")["status"], "completed")
        self.assertIsNone(board.state["active"])

    def test_native_handoff_is_not_automatically_retried_and_completed_success_can_be_verified(self):
        for boundary, position, expected in (("requested_pause", 4, "review"),
                                             ("decision_error", 4, "review"),
                                             ("requested_pause", 5, "completed")):
            with self.subTest(boundary=boundary, position=position):
                board = GoalBoard()
                board.add(destination())
                board.activate("destination", observed())
                context = board.begin(observed(), 0)["context"]
                result = board.finish(context["execution"]["id"], observed(position),
                                      {"boundary": boundary, "attempts": 0})
                self.assertEqual(result["reason"], expected)
                self.assertIsNone(board.state["active"])


class ConditionTest(TestCase):
    def test_unknown_is_not_false_and_boolean_is_not_a_numeric_match(self):
        condition = equals(["hero", "missing"], False)
        self.assertIsNone(check([condition], observed())["matches"])
        condition = {"path": ["count"], "op": "gte", "value": 1}
        self.assertIsNone(check([condition], {"count": True})["matches"])
        self.assertFalse(check([equals(["items"], [1])], {"items": [True]})["matches"])
        self.assertTrue(check([condition], {"count": 2})["matches"])


class RunnerTest(TestCase):
    def test_lost_response_requires_recovery_before_edits_or_execution_and_counts_attempt_once(self):
        board = GoalBoard()
        board.add(destination())
        board.activate("destination", observed())
        disk = {"board": board.snapshot()}

        class Client:
            executions = 0

            def snapshot(self):
                return {"state": "paused", "last": {"decision": 10}}, observed()

            def execute(self, preparation):
                self.executions += 1
                raise OSError("response lost after input")

            def recover(self, execution):
                return {"state": "paused"}, observed(), {"boundary": "caller_pause", "attempts": 1, "uncertain": True}

        client = Client()

        def save(document):
            disk.clear()
            disk.update(deepcopy(document))

        with self.assertRaises(OSError):
            step(deepcopy(disk), client, save)
        pending = GoalBoard(disk["board"])
        execution_id = pending.state["inflight"]["id"]
        for operation in (lambda: pending.remove("destination"),
                          lambda: step(deepcopy(disk), client, save)):
            with self.assertRaises(ValueError):
                operation()
        result, _ = step(deepcopy(disk), client, save, recover=True)
        self.assertEqual(result["reason"], "review")
        self.assertEqual(client.executions, 1)
        recovered = GoalBoard(disk["board"])
        self.assertIsNone(recovered.state["inflight"])
        self.assertEqual(recovered.goal("destination")["attempts_used"], 1)
        with self.assertRaises(ValueError):
            recovered.finish(execution_id, observed(), {"boundary": "action_budget", "attempts": 1})

    def test_already_satisfied_goal_does_not_call_executor(self):
        board = GoalBoard()
        board.add(destination())
        board.activate("destination", observed())

        class Client:
            def snapshot(self):
                return {"state": "paused"}, observed(5)

            def execute(self, preparation):
                raise AssertionError("goal was already satisfied")

        result, _ = step({"board": board.snapshot()}, Client(), lambda document: None)
        self.assertEqual(result["reason"], "completed")

    def test_rejected_window_sends_nothing_and_hands_back_without_recovery(self):
        board = GoalBoard()
        board.add(dict(destination(), tools=["flarp"]))
        board.activate("destination", observed())
        disk = {"board": board.snapshot()}

        class Client:
            def snapshot(self):
                return {"state": "paused", "last": {"decision": 3}}, observed()

            def execute(self, preparation):
                self.tools = preparation["tools"]
                raise Rejected("unknown tools: flarp")

        client = Client()
        result, _ = step(disk, client, lambda document: None)
        self.assertEqual((result["reason"], result["boundary"]), ("review", "harness_rejected"))
        self.assertEqual(client.tools, ["flarp"])
        rejected = GoalBoard(disk["board"])
        self.assertIsNone(rejected.state["inflight"])
        self.assertEqual(rejected.goal("destination")["attempts_used"], 0)
        rejected.update("destination", {"tools": ["travel"]})
