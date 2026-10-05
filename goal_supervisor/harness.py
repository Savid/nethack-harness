"""Caller-side bridge through the adapter's public command interface."""
import json
import subprocess
import time


class HarnessClient:
    def __init__(self, command, directory):
        self.command = list(command) + ["--dir", directory]

    def call(self, *arguments, payload=None, lines=False):
        result = subprocess.run(self.command + list(arguments), input=json.dumps(payload) if payload is not None else None,
                                capture_output=True, text=True, timeout=120)
        if result.returncode not in (0, 2, 3):
            raise ValueError(result.stderr.strip() or result.stdout.strip() or "harness command failed")
        if arguments[0] == "pause":
            return result.stdout.strip()
        return [json.loads(line) for line in result.stdout.splitlines()] if lines else json.loads(result.stdout)

    def observe(self):
        return self.call("observe")

    def status(self):
        return self.call("status")

    def paused(self):
        status = self.status()
        if not status.get("alive") or status.get("state") not in ("paused", "ended"):
            raise ValueError("the harness must be alive and paused before supervising goals")
        return status

    def execute(self, context):
        goal = context["goal"]
        limits = goal["limits"]
        response = self.call("resume", "--objective", goal["objective"], "--context-file", "-",
                             "--max-action-attempts", "1", "--max-action-steps", str(limits["steps_per_action"]),
                             "--review-after-calls", str(limits["decision_calls_per_action"]), "--timeout", "0",
                             payload=context)
        # CLI wait time is not an execution bound. Always establish a confirmed pause before returning.
        deadline = time.monotonic() + 60
        while response.get("status", response).get("state") not in ("paused", "ended"):
            if time.monotonic() >= deadline:
                self.call("pause")
                break
            response = self.call("wait", "--timeout", "1")
        return self.result(context["execution"])

    def recover(self, execution):
        status = self.status()
        if status.get("state") != "ended":
            self.call("pause")
        return self.result(execution)

    def result(self, execution):
        status = self.paused()
        observation = self.observe()
        records = self.call("export", "--after", str(execution["after_decision"]), lines=True)
        def belongs(record):
            context = record["request"]["state"].get("caller_context", {})
            owner = context.get("execution") if isinstance(context, dict) else None
            return record["source"] != "manual" and isinstance(owner, dict) and owner.get("id") == execution["id"]

        related = [record for record in records if belongs(record)]
        foreign = [record["id"] for record in records if record["source"] != "protocol" and record not in related]
        attempts = sum(any(item["source"] == "action" for item in record["inputs"]) for record in related)
        uncertain = bool(foreign or any(item["status"] != "completed" for record in related for item in record["inputs"]))
        last = next((record["outcome"] for record in reversed(related) if record["outcome"]), {})
        outcome = {"boundary": status.get("reason"), "execution_reason": last.get("execution_reason", last.get("reason")),
                   "attempts": attempts, "uncertain": uncertain,
                   "decisions": [record["id"] for record in related], "foreign_decisions": foreign,
                   "result": last}
        return observation, outcome
