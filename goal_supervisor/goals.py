"""Editable caller-owned goals, explicit activation, and evidence-based outcomes."""
from collections import Counter
from copy import deepcopy
import json
from uuid import uuid4

from .conditions import check, validate


CONDITIONS = ("success", "activate_when", "valid_while", "review_when")
TERMINAL = ("completed", "removed")
LIMITS = {"action_attempts": 8, "decision_calls_per_action": 8, "steps_per_action": 1, "idle_attempts": 4}
SPEC_FIELDS = {"id", "objective", "parent_id", "limits", "metadata", "review_on", "tools", *CONDITIONS}
WINDOWS = 16
ATTEMPTS_SHOWN = 12


def point(observation):
    return {"fingerprint": observation.get("fingerprint"), "phase": observation.get("phase"),
            "position": observation.get("hero", {}).get("position"),
            "turn": observation.get("hero", {}).get("turn"), "level": (observation.get("level") or {}).get("id")}


def attempt(window):
    outcome = window["outcome"]
    result = outcome.get("result", {})
    action = result.get("action") or {}
    entry = {"action": action.get("id"), "from": window["before"]["position"], "to": window["after"]["position"],
             "result": outcome.get("execution_reason") or outcome.get("boundary")}
    if window["before"]["level"] != window["after"]["level"]:
        entry["level"] = [window["before"]["level"], window["after"]["level"]]
    if isinstance(result.get("elapsed_turns"), int):
        entry["turns"] = result["elapsed_turns"]
    for key, name in (("changed_fields", "changed"), ("new_terrain", "new_terrain")):
        if result.get(key):
            entry[name] = result[key]
    if outcome.get("uncertain"):
        entry["uncertain"] = True
    return entry, action.get("target")


def idle(window):
    """The attempt left position, level and turn as they were (an unknown turn cannot show progress)."""
    before, after = window["before"], window["after"]
    return (before["position"] == after["position"] and before["level"] == after["level"] and
            (before["turn"] == after["turn"] or before["turn"] is None or after["turn"] is None))


def idle_streak(goal):
    """Consecutive idle attempts since the goal was last activated."""
    count = 0
    for window in reversed(goal["recent_windows"]):
        if window["sequence"] <= goal["activated_after_window"]:
            break
        if not window["outcome"]["attempts"]:
            continue
        if not idle(window):
            break
        count += 1
    return count


def history(goal):
    """Recent attempts under this goal and actions it selected more than once, with what they achieved."""
    attempts = [attempt(window) for window in goal["recent_windows"] if window["outcome"]["attempts"]]
    tries = Counter(entry["action"] for entry, _ in attempts)
    repeated = [{"action": action, "tries": count,
                 "moved": sum(entry["from"] != entry["to"] for entry, _ in attempts if entry["action"] == action),
                 "reached_target": sum(entry["to"] == target for entry, target in attempts
                                       if entry["action"] == action and target is not None)}
                for action, count in tries.items() if count > 1]
    shown = [entry for entry, _ in attempts[-ATTEMPTS_SHOWN:]]
    return {"recent_attempts": shown, "omitted_attempts": goal["attempts_used"] - len(shown),
            "repeated_actions": repeated, "idle_streak": idle_streak(goal),
            "idle_meaning": "consecutive attempts that changed neither position, level nor turn"}


def completion(goal, observation):
    if not goal["success"]:
        return {"matches": None, "reason": "caller_confirmation_required"}
    checked = check(goal["success"], observation)
    return {"matches": checked["matches"], "unmet": [
        dict(item["condition"], observed=item["observed"]) for item in checked["evidence"] if item["matches"] is not True]}


def summary(goal):
    keys = ("id", "objective", "success", "valid_while", "review_when", "attempts_used")
    result = {key: deepcopy(goal[key]) for key in keys}
    result["attempts_remaining"] = goal["limits"]["action_attempts"] - goal["attempts_used"]
    for key in ("metadata", "tools"):
        if goal[key]:
            result[key] = deepcopy(goal[key])
    return result


def stored_outcome(outcome):
    """Window outcome retained in goal state, without per-key frames and terrain listings."""
    result = {key: value for key, value in outcome.get("result", {}).items() if key not in ("frames", "keys")}
    if isinstance(result.get("new_terrain"), list):
        result["new_terrain"] = len(result["new_terrain"])
    return dict(outcome, result=result)


class GoalBoard:
    def __init__(self, state=None):
        self.state = deepcopy(state) if state is not None else {
            "goals": {}, "active": None, "inflight": None, "events": []}

    def snapshot(self):
        return deepcopy(self.state)

    def event(self, kind, goal, **data):
        self.state["events"].append(dict(sequence=len(self.state["events"]) + 1, kind=kind,
                                         goal_id=goal["id"], revision=goal["revision"], **deepcopy(data)))

    def editable(self):
        if self.state["inflight"]:
            raise ValueError("an execution window is unresolved; recover it before changing goals")

    def goal(self, identifier):
        if not isinstance(identifier, str) or identifier not in self.state["goals"]:
            raise ValueError("unknown goal: " + str(identifier))
        return self.state["goals"][identifier]

    def chain(self, identifier):
        result = []
        while identifier is not None:
            goal = self.goal(identifier)
            result.append(goal)
            identifier = goal["parent_id"]
        return list(reversed(result))

    def descendants(self, identifier):
        return [goal for goal in self.state["goals"].values()
                if any(parent["id"] == identifier for parent in self.chain(goal["id"]))]

    def spec(self, spec):
        if not isinstance(spec, dict) or set(spec) - SPEC_FIELDS:
            raise ValueError("unknown goal fields")
        spec = deepcopy(spec)
        identifier = spec.setdefault("id", uuid4().hex)
        if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 128:
            raise ValueError("goal id must be nonempty text, at most 128 characters")
        if not isinstance(spec.get("objective"), str) or not spec["objective"].strip():
            raise ValueError("goal objective must be nonempty text")
        parent = spec.setdefault("parent_id", None)
        if parent is not None:
            if parent == identifier or self.goal(parent)["status"] in TERMINAL:
                raise ValueError("parent must be another unfinished goal")
            if any(item["id"] == identifier for item in self.chain(parent)):
                raise ValueError("goal parents cannot form a cycle")
        for key in CONDITIONS:
            validate(spec.setdefault(key, []))
        reasons = spec.setdefault("review_on", [])
        if not isinstance(reasons, list) or not all(isinstance(reason, str) and reason for reason in reasons):
            raise ValueError("review_on must be a list of execution reasons")
        limits = spec.setdefault("limits", {})
        if not isinstance(limits, dict) or set(limits) - LIMITS.keys():
            raise ValueError("unknown goal limits")
        spec["limits"] = dict(LIMITS, **limits)
        for key, value in spec["limits"].items():
            if type(value) is not int or value <= 0 or key == "steps_per_action" and value > 64:
                raise ValueError("goal limits must be positive integers; steps_per_action cannot exceed 64")
        if not isinstance(spec.setdefault("metadata", {}), dict):
            raise ValueError("goal metadata must be an object")
        tools = spec.setdefault("tools", [])
        if not isinstance(tools, list) or not all(isinstance(tool, str) and tool.strip() and "," not in tool
                                                  for tool in tools):
            raise ValueError("tools must be a list of tool names")
        try:
            json.dumps(spec, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("goals must contain finite JSON values") from None
        return spec

    def add(self, spec):
        self.editable()
        spec = self.spec(spec)
        if spec["id"] in self.state["goals"]:
            raise ValueError("goal id already exists; update or replace it explicitly")
        goal = dict(spec, revision=1, status="pending", attempts_used=0, windows_used=0, recent_windows=[],
                    activated_after_window=0)
        self.state["goals"][goal["id"]] = goal
        self.event("added", goal, spec=spec)
        if self.state["active"] == goal["parent_id"] and goal["parent_id"] is not None:
            self.suspend(goal["parent_id"], "child_added")
        return deepcopy(goal)

    def update(self, identifier, patch):
        self.editable()
        goal = self.goal(identifier)
        if goal["status"] in TERMINAL:
            raise ValueError("replace a finished or removed goal instead of updating it")
        if not isinstance(patch, dict) or "id" in patch or set(patch) - SPEC_FIELDS:
            raise ValueError("update accepts goal fields except id")
        spec = {key: deepcopy(goal[key]) for key in SPEC_FIELDS}
        spec.update(deepcopy(patch))
        if "limits" in patch and isinstance(patch["limits"], dict):
            spec["limits"] = dict(goal["limits"], **patch["limits"])
        spec = self.spec(spec)
        active = self.state["active"]
        if active and any(parent["id"] == identifier for parent in self.chain(active)):
            self.suspend(active, "goal_updated")
        goal.update(spec)
        goal["revision"] += 1
        if goal["parent_id"] is not None and self.state["active"] == goal["parent_id"]:
            self.suspend(goal["parent_id"], "child_reparented")
        self.event("updated", goal, spec=spec)
        return deepcopy(goal)

    def suspend(self, identifier, reason="caller_suspended", evidence=None):
        self.editable()
        goal = self.goal(identifier)
        if goal["status"] in TERMINAL:
            raise ValueError("cannot suspend a finished or removed goal")
        active = self.state["active"]
        if active and any(parent["id"] == identifier for parent in self.chain(active)):
            if active != identifier:
                child = self.goal(active)
                child["status"] = "suspended"
                self.event("suspended", child, reason="ancestor_suspended")
            self.state["active"] = None
        goal["status"] = "suspended"
        self.event("suspended", goal, reason=reason, evidence=evidence)
        return deepcopy(goal)

    def remove(self, identifier):
        self.editable()
        self.goal(identifier)
        removed = []
        for goal in self.descendants(identifier):
            if goal["status"] != "removed":
                if self.state["active"] == goal["id"]:
                    self.state["active"] = None
                goal["status"] = "removed"
                self.event("removed", goal, reason="caller_removed_subtree", root=identifier)
                removed.append(goal["id"])
        return removed

    def replace(self, identifier, spec):
        self.editable()
        previous = self.goal(identifier)
        if not isinstance(spec, dict):
            raise ValueError("replacement must be a goal object")
        replacement = dict(spec)
        replacement.setdefault("parent_id", previous["parent_id"])
        replacement = self.spec(replacement)
        if replacement["id"] in self.state["goals"]:
            raise ValueError("a replacement needs a new id")
        if replacement["parent_id"] in {goal["id"] for goal in self.descendants(identifier)}:
            raise ValueError("replacement cannot be a child of the removed subtree")
        self.remove(identifier)
        result = self.add(replacement)
        self.event("replaced", previous, replacement_id=result["id"])
        return result

    def complete(self, identifier, observation, evidence):
        self.editable()
        goal = self.goal(identifier)
        if goal["status"] in TERMINAL:
            raise ValueError("goal is already finished or removed")
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("caller-confirmed completion requires an explanation of the evidence")
        if any(child["id"] != identifier and child["status"] not in TERMINAL for child in self.descendants(identifier)):
            raise ValueError("finish or remove unfinished children before completing their parent")
        goal["status"] = "completed"
        if self.state["active"] == identifier:
            self.state["active"] = None
        self.event("completed", goal, source="caller_confirmation", evidence=evidence, observation=point(observation))
        return deepcopy(goal)

    def eligibility(self, identifier, observation):
        goal = self.goal(identifier)
        if goal["status"] in TERMINAL:
            return {"eligible": False, "reason": goal["status"]}
        if any(child["id"] != identifier and child["status"] not in TERMINAL for child in self.descendants(identifier)):
            return {"eligible": False, "reason": "unfinished_children"}
        for parent in self.chain(identifier):
            for name in ("activate_when", "valid_while"):
                result = check(parent[name], observation)
                if result["matches"] is not True:
                    return {"eligible": result["matches"], "reason": name, "goal_id": parent["id"], **result}
        return {"eligible": True}

    def activate(self, identifier, observation):
        self.editable()
        eligible = self.eligibility(identifier, observation)
        if eligible["eligible"] is not True:
            return dict(eligible, goal_id=identifier)
        if self.state["active"] and self.state["active"] != identifier:
            self.suspend(self.state["active"], "caller_switched")
        goal = self.goal(identifier)
        goal["status"], self.state["active"] = "active", identifier
        goal["activated_after_window"] = goal["windows_used"]
        self.event("activated", goal, observation=point(observation))
        return self.assess(observation)

    def assess(self, observation, outcome=None):
        self.editable()
        if not self.state["active"]:
            return {"reason": "no_active_goal"}
        goal = self.goal(self.state["active"])
        result = {"goal_id": goal["id"], "observation": point(observation)}
        for parent in self.chain(goal["id"]):
            for name, any_match in (("valid_while", False), ("review_when", True)):
                checked = check(parent[name], observation, any_match)
                if checked["matches"] is None or checked["matches"] == any_match:
                    evidence = dict(checked, condition_group=name, condition_goal_id=parent["id"])
                    self.suspend(goal["id"], "condition_unknown" if checked["matches"] is None else name, evidence)
                    return dict(result, reason="review", **evidence)
            if outcome and set(parent["review_on"]) & {outcome.get("boundary"), outcome.get("execution_reason")}:
                self.suspend(goal["id"], "review_on", outcome)
                return dict(result, reason="review", condition_goal_id=parent["id"], outcome=outcome)
        if goal["success"]:
            success = check(goal["success"], observation)
            if success["matches"] is True:
                goal["status"], self.state["active"] = "completed", None
                self.event("completed", goal, source="observation_conditions", evidence=success, observation=point(observation))
                return dict(result, reason="completed", **success)
            if success["matches"] is None:
                self.suspend(goal["id"], "success_unknown", success)
                return dict(result, reason="review", condition_group="success", **success)
        if observation.get("phase") == "ended":
            self.suspend(goal["id"], "game_over", point(observation))
            return dict(result, reason="review", boundary="game_over")
        if goal["attempts_used"] >= goal["limits"]["action_attempts"]:
            self.suspend(goal["id"], "goal_budget_exhausted")
            return dict(result, reason="review", boundary="goal_budget_exhausted")
        if idle_streak(goal) >= goal["limits"]["idle_attempts"]:
            self.suspend(goal["id"], "idle_attempts")
            return dict(result, reason="review", boundary="idle_attempts")
        return dict(result, reason="ready")

    def begin(self, observation, after_decision):
        assessment = self.assess(observation)
        if assessment["reason"] != "ready":
            return assessment
        goal = self.goal(self.state["active"])
        execution = {"id": uuid4().hex, "goal_id": goal["id"], "revision": goal["revision"],
                     "after_decision": after_decision, "before": point(observation)}
        context = {"goal": summary(goal), "completion": completion(goal, observation), "history": history(goal),
                   "execution": {"id": execution["id"]}}
        parents = self.chain(goal["id"])[:-1]
        if parents:
            context["parents"] = [{key: deepcopy(parent[key]) for key in
                                   ("id", "objective", "valid_while", "review_when", "metadata")} for parent in parents]
        self.state["inflight"] = deepcopy(execution)
        self.event("execution_started", goal, execution=execution)
        # The first window after activation starts a new harness scope; later ones continue it, so the
        # harness's repetition and movement-cycle evidence spans the goal's windows.
        return {"reason": "execute", "context": context, "objective": goal["objective"],
                "limits": deepcopy(goal["limits"]), "tools": list(goal["tools"]),
                "continue_scope": goal["windows_used"] > goal["activated_after_window"]}

    def reject(self, execution_id, error):
        """The harness refused the window before any input, so nothing was attempted."""
        execution = self.state["inflight"]
        if not execution or execution["id"] != execution_id:
            raise ValueError("result does not match the pending execution")
        goal = self.goal(execution["goal_id"])
        self.state["inflight"] = None
        self.event("execution_rejected", goal, execution_id=execution_id, error=error)
        self.suspend(goal["id"], "harness_rejected", {"error": error})
        return {"reason": "review", "goal_id": goal["id"], "boundary": "harness_rejected", "error": error}

    def latest(self):
        """The goal most recently activated, executed, suspended or completed."""
        for event in reversed(self.state["events"]):
            if event["kind"] in ("activated", "execution_finished", "suspended", "completed"):
                goal = self.state["goals"][event["goal_id"]]
                return {key: deepcopy(goal[key]) for key in
                        ("id", "status", "objective", "attempts_used", "limits", "tools")}
        return None

    def finish(self, execution_id, observation, outcome, recovered=False):
        execution = self.state["inflight"]
        if not execution or execution["id"] != execution_id:
            raise ValueError("result does not match the pending execution")
        goal = self.goal(execution["goal_id"])
        if goal["revision"] != execution["revision"]:
            raise ValueError("goal changed during execution")
        attempts = outcome.get("attempts")
        if type(attempts) is not int or attempts < 0:
            raise ValueError("execution result needs a nonnegative attempt count")
        goal["attempts_used"] += attempts
        goal["windows_used"] += 1
        window = dict(execution, outcome=stored_outcome(outcome), after=point(observation),
                      sequence=goal["windows_used"])
        goal["recent_windows"] = (goal["recent_windows"] + [window])[-WINDOWS:]
        self.event("execution_finished", goal, result=window, recovered=recovered)
        self.state["inflight"] = None
        if recovered or outcome.get("uncertain"):
            self.suspend(goal["id"], "recovered" if recovered else "execution_handoff", outcome)
            return {"reason": "review", "goal_id": goal["id"], "outcome": outcome}
        assessment = self.assess(observation, outcome)
        if assessment["reason"] == "ready" and outcome.get("boundary") != "action_budget":
            self.suspend(goal["id"], "execution_handoff", outcome)
            return {"reason": "review", "goal_id": goal["id"], "outcome": outcome}
        return assessment
