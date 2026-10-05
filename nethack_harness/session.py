"""Observe, ask the decision engine, execute its choice, and record the result."""
import json

from .actions import catalogue
from .base import Paused
from .decide import request_body, DecisionError
from .execute import Boundary, Executor
from .perceive import Observer
from .progress import NavigationProgress, ObjectiveProgress
from .transport import Closed, Held


class Session:
    def __init__(self, term, engine, store, settings):
        self.term, self.engine, self.store, self.settings = term, engine, store, settings
        self.observer = Observer()
        self.calls = self.actions = self.turns = 0
        self.best_depth = 0
        self.last = None
        self.pending_tool = None
        self.failed_action = None
        self.navigation = NavigationProgress()
        self.review_calls = 0
        self.objective_progress = ObjectiveProgress()

    def resume(self, continue_scope=False):
        """Renew the caller budgets. A new scope also forgets attempts, movement cycles and no-effect evidence,
        so the caller's resume permits another try; a continued scope keeps them for an unchanged objective."""
        self.pending_tool = None
        self.review_calls = 0
        if continue_scope:
            self.objective_progress.renew()
            return
        self.failed_action = None
        self.navigation.reset()
        self.objective_progress = ObjectiveProgress()

    def observe(self):
        observation = self.observer.observation(self.term.view())
        self.objective_progress.sync(observation, self.settings.objective)
        observation["objective_progress"] = self.objective_progress.snapshot(self.settings.max_action_attempts)
        self.navigation.sync(observation, self.settings.objective)
        observation["navigation_progress"] = self.navigation.snapshot()
        limit = self.settings.review_after_calls
        observation["review_budget"] = {"limit_calls": limit, "calls_used": self.review_calls,
                                        "calls_remaining": max(0, limit - self.review_calls) if limit else None}
        depth = observation["hero"].get("dlvl")
        if depth is not None:
            self.best_depth = max(self.best_depth, depth)
        return observation

    def offered(self):
        return catalogue(self.term.view(), self.observer, self.settings.max_action_steps)

    def step(self, cancelled=lambda: False, manual=None, protocol=None):
        self.term.poll()
        before = self.observe()
        if before["phase"] == "ended":
            return "game_over"
        actions = self.offered()
        supplied = manual or protocol
        if not supplied and before["phase"] == "play" and self.settings.tools:
            actions = [action for action in actions if action.tool in self.settings.tools or action.kind == "pause"]
        if supplied and supplied.kind in ("manual", "redraw"):
            actions.append(supplied)
        source = "manual" if manual else "protocol" if protocol or before["phase"] == "more" else "engine"
        if source == "engine" and self.objective_progress.exhausted(self.settings.max_action_attempts):
            self.pending_tool = None
            self.store.write("observation", before)
            return "action_budget"
        if source == "engine" and self.settings.review_after_calls and self.review_calls >= self.settings.review_after_calls:
            self.pending_tool = None
            self.store.write("observation", before)
            return "review_budget"
        selection_key = (before["fingerprint"], self.settings.objective,
                         json.dumps(self.settings.caller_context, allow_nan=False), tuple(actions))
        tool = None
        if self.pending_tool and self.pending_tool[0] == selection_key and source == "engine":
            tool = self.pending_tool[1]
        self.pending_tool = None
        request = request_body(before, actions, self.settings.objective, self.engine.model, tool,
                               self.settings.caller_context)
        number = self.store.begin(source, request)
        attempt = None
        sent = {}

        def record_send(keys, src):
            nonlocal attempt
            if src == "action" and attempt is None and source == "engine":
                if self.objective_progress.exhausted(self.settings.max_action_attempts):
                    raise Boundary("action_budget")
            input_number = self.store.input(number, keys, src)
            if src == "action" and attempt is None:
                # Reserve before I/O: a write with an uncertain result still consumes an attempt.
                attempt = self.objective_progress.begin(number, source, action, before)
            if attempt is not None:
                item = {"keys": keys, "source": src, "status": "pending"}
                attempt["inputs"].append(item)
                sent[input_number] = item
            return input_number

        def record_result(input_number, status):
            self.store.input_status(input_number, status)
            if input_number in sent:
                sent[input_number]["status"] = status

        executor = Executor(self.term, self.observer, record_send, record_result)
        outcome = None
        try:
            if supplied:
                action = supplied
                response, latency = None, 0
            elif source == "protocol":
                action, response, latency = actions[0], None, 0
            else:
                self.calls += 1
                self.review_calls += 1
                choice, response, latency = self.engine.choose(request)
                self.store.choice(number, choice, response, latency)
                if request["state"]["decision"]["stage"] == "tool":
                    matching = [a for a in actions if a.tool == choice]
                    if len(matching) > 1:
                        interrupted = cancelled()
                        if not interrupted:
                            self.pending_tool = (selection_key, choice)
                        outcome = {"reason": "caller_interrupt" if interrupted else "tool_selected",
                                   "tool": choice, "steps": 0}
                        action = None
                    else:
                        action = matching[0]
                else:
                    action = next(a for a in actions if a.id == choice)
            if source != "engine":
                self.store.choice(number, action.id, response, latency)
            if action is not None:
                if action not in actions:
                    outcome = {"reason": "observation_changed", "steps": 0, "keys": [], "frames": []}
                elif source == "engine" and self.failed_action and self.failed_action[0] == (
                        before["fingerprint"], self.settings.objective, action):
                    outcome = {"reason": "repeated_no_effect", "steps": 0, "keys": [], "frames": [],
                               "review": {"previous_decision": self.failed_action[1],
                                          "evidence": "same action selected again after no observed effect on the same screen",
                                          "objective": self.settings.objective, "selected_action": action.id}}
                    self.pending_tool = None
                else:
                    outcome = executor.run(action, before["fingerprint"], cancelled)
                outcome["action"] = action.as_dict()
                self.actions += int(outcome["steps"] > 0 and source != "protocol")
                self.turns += max(0, outcome.get("elapsed_turns") or 0)
        except DecisionError as e:
            self.store.choice(number, None, e.response, e.latency)
            outcome = {"reason": "decision_error", "error": str(e), "steps": 0}
        except Held:
            outcome = {"reason": "input_held", "error": "game input is temporarily unavailable"}
        except Closed:
            outcome = {"reason": "terminal_closed", "error": "terminal connection ended"}
        except Paused as e:
            outcome = {"reason": "execution_paused", "error": str(e)}
        except Exception as e:
            outcome = {"reason": "execution_error", "error": type(e).__name__}
        after = self.observe()
        if attempt is not None:
            attempt["after"] = self.objective_progress.point(after)
            attempt["result"] = {key: outcome[key] for key in
                                 ("reason", "steps", "elapsed_turns", "changed_fields", "error") if key in outcome}
            after["objective_progress"] = self.objective_progress.snapshot(self.settings.max_action_attempts)
        if source == "engine" and outcome.get("action"):
            review = self.navigation.record(before, after, self.settings.objective, action, outcome, number)
            if review and outcome["reason"] in ("completed", "observation_changed", "step_limit",
                                               "exploration_boundary", "branch_discovered", "feature_discovered"):
                outcome["execution_reason"] = outcome["reason"]
                outcome["reason"] = "navigation_cycle"
                outcome["review"] = review
            after["navigation_progress"] = self.navigation.snapshot()
        elif outcome.get("steps"):
            self.navigation.reset()
            self.navigation.sync(after, self.settings.objective)
            after["navigation_progress"] = self.navigation.snapshot()
        normal_boundary = outcome["reason"] in ("completed", "observation_changed", "step_limit", "tool_selected",
                                                "exploration_boundary", "branch_discovered", "feature_discovered", "prompt",
                                                "movement_interrupted", "route_changed", "no_observed_effect")
        if (source == "engine" and self.objective_progress.exhausted(self.settings.max_action_attempts)
                and normal_boundary):
            outcome["execution_reason"] = outcome["reason"]
            outcome["reason"] = "action_budget"
            outcome["review"] = {"evidence": "caller-selected action attempt budget reached",
                                 "objective_id": self.objective_progress.id,
                                 "attempts": self.objective_progress.since_resume(),
                                 "limit_attempts": self.settings.max_action_attempts}
            self.pending_tool = None
        elif (source == "engine" and self.settings.review_after_calls and
                self.review_calls >= self.settings.review_after_calls and
                normal_boundary):
            outcome["execution_reason"] = outcome["reason"]
            outcome["reason"] = "review_budget"
            outcome["review"] = {"evidence": "caller-selected decision call budget reached",
                                 "calls": self.review_calls, "limit_calls": self.settings.review_after_calls}
            self.pending_tool = None
        if outcome["reason"] in ("requested_pause", "navigation_cycle", "review_budget", "action_budget",
                                 "repeated_no_effect", "unexpected_prompt"):
            outcome.setdefault("review", {}).update(objective=self.settings.objective,
                decision_stage=request["state"]["decision"]["stage"], selected_tool=request["state"]["decision"]["tool"],
                observation_fingerprint=after["fingerprint"])
        if outcome.get("execution_reason", outcome["reason"]) == "no_observed_effect" and source == "engine":
            self.failed_action = ((after["fingerprint"], self.settings.objective, action), number)
        elif outcome.get("steps", 0) > 0:
            self.failed_action = None
        outcome["terminal"] = after["phase"] == "ended"
        self.store.finish(number, outcome, after)
        self.last = {"decision": number, "source": source, "outcome": outcome}
        self.store.write("observation", after)
        if outcome["terminal"]:
            return "game_over"
        if outcome["reason"] in ("requested_pause", "decision_error", "input_held", "terminal_closed",
                                 "execution_paused", "execution_error", "repeated_no_effect", "unexpected_prompt",
                                 "navigation_cycle", "review_budget", "action_budget"):
            return outcome["reason"]
        return None
