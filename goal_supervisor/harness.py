"""Caller-side bridge through the adapter's public command interface."""
import json
import subprocess
import time

WINDOW_WAIT = 120


class Rejected(ValueError):
    """The harness rejected the command's arguments before contacting the session."""


class HarnessClient:
    def __init__(self, command, directory):
        self.command = list(command) + ["--dir", directory]

    def call(self, *arguments, payload=None, lines=False):
        result = subprocess.run(self.command + list(arguments), input=json.dumps(payload) if payload is not None else None,
                                capture_output=True, text=True, timeout=WINDOW_WAIT + 60)
        if result.returncode == 64:
            raise Rejected(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "invalid harness arguments")
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

    def snapshot(self):
        """Status and observation of a paused session in one call."""
        response = self.call("wait", "--timeout", "0")
        status = response.get("status", response)
        if not status.get("alive") or status.get("state") not in ("paused", "ended"):
            raise ValueError("the harness must be alive and paused before supervising goals")
        return response["status"], response["observation"]

    def execute(self, preparation):
        execution = preparation["context"]["execution"]
        limits = preparation["limits"]
        response = self.call("resume", "--objective", preparation["objective"], "--context-file", "-",
                             "--max-action-attempts", "1", "--max-action-steps", str(limits["steps_per_action"]),
                             "--review-after-calls", str(limits["decision_calls_per_action"]),
                             "--tools", ",".join(preparation["tools"]) or "all",
                             "--records-after", str(preparation["after_decision"]), "--timeout", str(WINDOW_WAIT),
                             *(["--continue-scope"] if preparation.get("continue_scope") else []),
                             payload=preparation["context"])
        if "records" in response:
            return self.outcome(execution, response["status"], response["observation"], response["records"])
        # CLI wait time is not an execution bound. Always establish a confirmed pause before returning.
        deadline = time.monotonic() + 60
        self.call("pause")
        while self.status().get("state") not in ("paused", "ended"):
            if time.monotonic() >= deadline:
                raise ValueError("the harness did not pause; recover this execution")
            time.sleep(0.2)
        return self.result(execution, preparation["after_decision"])

    def record_goals(self, transitions):
        """Append goal transitions to the session's caller intent records."""
        self.call("goal", "--file", "-", payload=transitions)

    def recover(self, execution):
        status = self.status()
        if status.get("state") != "ended":
            self.call("pause")
        return self.result(execution, execution["after_decision"])

    def result(self, execution, after_decision):
        status = self.paused()
        observation = self.observe()
        records = self.call("export", "--after", str(after_decision), lines=True)
        return self.outcome(execution, status, observation, records)

    @staticmethod
    def outcome(execution, status, observation, records):
        def belongs(record):
            context = record["request"]["state"].get("caller_context", {})
            owner = context.get("execution") if isinstance(context, dict) else None
            return record["source"] != "manual" and isinstance(owner, dict) and owner.get("id") == execution["id"]

        records = [record for record in records if record["type"] == "decision"]
        related = [record for record in records if belongs(record)]
        foreign = [record["id"] for record in records if record["source"] != "protocol" and record not in related]
        attempts = sum(any(item["source"] == "action" for item in record["inputs"]) for record in related)
        uncertain = bool(foreign or any(item["status"] != "completed" for record in related for item in record["inputs"]))
        last = next((record["outcome"] for record in reversed(related) if record["outcome"]), {})
        outcome = {"boundary": status.get("reason"), "execution_reason": last.get("execution_reason", last.get("reason")),
                   "attempts": attempts, "uncertain": uncertain,
                   "decisions": [record["id"] for record in related], "foreign_decisions": foreign,
                   "result": last}
        return status, observation, outcome
