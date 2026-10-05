"""Objective-scoped execution evidence and bounded movement history."""
from collections import deque
from copy import deepcopy
from uuid import uuid4


class ObjectiveProgress:
    def __init__(self):
        self.objective = None
        self.id = None
        self.start = None
        self.attempts = 0
        self.events = deque(maxlen=32)

    @staticmethod
    def point(observation):
        return {"fingerprint": observation["fingerprint"], "phase": observation["phase"],
                "position": observation["hero"].get("position"), "turn": observation["hero"].get("turn"),
                "level": (observation.get("level") or {}).get("id")}

    def sync(self, observation, objective):
        if self.start is None or objective != self.objective:
            self.objective, self.id = objective, uuid4().hex
            self.start = self.point(observation)
            self.attempts = 0
            self.events.clear()

    def exhausted(self, limit):
        return bool(limit and self.attempts >= limit)

    def begin(self, decision, source, action, before):
        self.attempts += 1
        event = {"attempt": self.attempts, "decision": decision, "source": source,
                 "action": action.as_dict(), "before": self.point(before), "inputs": []}
        self.events.append(event)
        return event

    def snapshot(self, limit):
        return deepcopy({"id": self.id, "start": self.start, "attempts_used": self.attempts,
                         "limit_attempts": limit, "attempts_remaining": max(0, limit - self.attempts) if limit else None,
                         "recent_attempts": list(self.events), "omitted_attempts": self.attempts - len(self.events)})


class NavigationProgress:
    def __init__(self):
        self.context = None
        self.positions = deque(maxlen=33)
        self.events = deque(maxlen=32)

    @staticmethod
    def key(observation, objective):
        level = observation.get("level") or {}
        obstacles = tuple((tuple(cell["position"]), cell["colour"]) for cell in level.get("structural_obstacles", ()))
        return (objective, level.get("id"), tuple(level.get("known_terrain", ())), obstacles)

    def reset(self):
        self.context = None
        self.positions.clear()
        self.events.clear()

    def sync(self, observation, objective):
        context = self.key(observation, objective)
        if observation["phase"] != "play" or context != self.context:
            self.reset()
            self.context = context
        if not self.positions and observation["hero"].get("position"):
            self.positions.append(observation["hero"]["position"])

    def record(self, before, after, objective, action, outcome, decision):
        if not outcome.get("steps"):
            return None
        if (action.kind not in ("move", "travel", "explore") or before["phase"] != "play" or
                after["phase"] != "play" or self.key(before, objective) != self.key(after, objective)):
            self.reset()
            self.sync(after, objective)
            return None
        start, end = before["hero"]["position"], after["hero"]["position"]
        if not start or not end or start == end:
            return None
        self.events.append({"decision": decision, "action": action.id, "target": outcome["action"]["target"],
                            "position_before": start, "position_after": end,
                            "turn_before": before["hero"].get("turn"), "turn_after": after["hero"].get("turn"),
                            "execution_reason": outcome.get("reason"), "steps": outcome["steps"],
                            "changed_fields": outcome.get("changed_fields", [])})
        self.positions.append(end)
        positions = list(self.positions)
        # Require two complete repetitions: a single return can be ordinary backtracking.
        for period in range(2, (len(positions) - 1) // 2 + 1):
            tail = positions[-(2 * period + 1):]
            if tail[0] == tail[period] == tail[-1] and tail[:period] == tail[period:2 * period]:
                return {"evidence": "same cycle of action endpoint positions completed twice without a change in remembered terrain",
                        "objective": objective, "level": after["level"]["id"],
                        "cycle": tail[:period + 1], "repetitions": 2,
                        "actions": list(self.events)[-2 * period:]}
        return None

    def snapshot(self):
        return {"scope": "consecutive movement on unchanged remembered terrain under the current objective",
                "recent_positions": list(self.positions), "recent_actions": list(self.events),
                "window_actions": 32}
