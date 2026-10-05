from unittest import TestCase
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from helpers import Endpoint, FakeGame
from goal_supervisor.goals import GoalBoard
from goal_supervisor.harness import HarnessClient
from goal_supervisor.runner import step
from goal_supervisor.storage import Repository


ROOT = Path(__file__).resolve().parent.parent


def frame(column, turn):
    row = list("|.....|")
    row[column - 20] = "@"
    return ("\x1b[H\x1b[2J\x1b[3;20H-------\x1b[4;20H" + "".join(row) + "\x1b[5;20H-------"
            "\x1b[23;1HHero the Stripling   St:16 Dx:12 Co:14 In:9 Wi:10 Ch:8 Lawful"
            "\x1b[24;1HDlvl:1 $:0 HP:12(16) Pw:2(2) AC:6 Xp:1 T:%d\x1b[4;%dH" % (turn, column)).encode()


def spec(identifier, column, attempts=4, parent=None):
    return {"id": identifier, "parent_id": parent, "objective": "Reach the selected position, then pause.",
            "success": [{"path": ["level", "id"], "op": "eq", "value": "level-1"},
                        {"path": ["hero", "position"], "op": "eq", "value": [4, column]}],
            "limits": {"action_attempts": attempts}}


class SupervisorBridgeTest(TestCase):
    def test_foreign_context_cannot_trap_recovery_or_consume_owned_attempts(self):
        observation = {"phase": "play", "hero": {"position": [4, 22]}, "level": {"id": "level-1"}}
        for context in ({}, {"execution": None}, {"execution": 1}, {"execution": "other"}, {"execution": []}):
            with self.subTest(context=context):
                board = GoalBoard()
                board.add(spec("destination", 23))
                board.activate("destination", observation)
                execution = board.begin(observation, 0)["context"]["execution"]
                records = [
                    {"id": 1, "type": "decision", "source": "engine", "request": {"state": {"caller_context": {
                        "execution": {"id": execution["id"]}}}},
                     "inputs": [{"source": "action", "status": "completed", "keys": "l"}],
                     "outcome": {"reason": "action_budget"}},
                    {"id": 2, "type": "decision", "source": "engine", "request": {"state": {"caller_context": context}},
                     "inputs": [{"source": "action", "status": "completed", "keys": "h"}],
                     "outcome": {"reason": "action_budget"}}]

                class Client(HarnessClient):
                    def __init__(self):
                        self.commands = []

                    def status(self):
                        return {"state": "paused", "alive": True, "reason": "caller_pause"}

                    def observe(self):
                        return observation

                    def call(self, *arguments, **kwargs):
                        self.commands.append(arguments[0])
                        return records if arguments[0] == "export" else "paused"

                document = {"board": board.snapshot()}
                client = Client()
                result, _ = step(document, client, lambda _: None, recover=True)
                recovered = GoalBoard(document["board"])
                self.assertEqual(result["reason"], "review")
                self.assertEqual(result["outcome"]["foreign_decisions"], [2])
                self.assertEqual(result["outcome"]["attempts"], 1)
                self.assertIsNone(recovered.state["inflight"])
                self.assertEqual(recovered.goal("destination")["status"], "suspended")
                self.assertEqual(recovered.goal("destination")["attempts_used"], 1)
                self.assertEqual(client.commands, ["pause", "export"])
                recovered.update("destination", {"metadata": {"reviewed": True}})


class SupervisorIntegrationTest(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.session = str(Path(self.directory.name) / "session")
        self.goals = str(Path(self.directory.name) / "goals")
        self.game = FakeGame([frame(column, 10 + column - 22) for column in range(22, 26)])

        def choose(request):
            choice = "move" if request["state"]["decision"]["stage"] == "tool" else "move:l"
            return {"answers": {"action": {"choice": choice}}}

        self.endpoint = Endpoint(choose)
        self.harness("start", "--paused", "--socket", self.game.path, "--decide", self.endpoint.url, "--timeout", "5",
                     "--quiet", "0.02")
        self.supervise("init", "--session", self.session)

    def tearDown(self):
        try:
            self.harness("stop")
            for _ in range(50):
                if not self.harness("status").get("alive"):
                    break
                time.sleep(0.05)
        finally:
            self.endpoint.close()
            self.game.close()
            self.directory.cleanup()

    def harness(self, *args):
        result = subprocess.run([sys.executable, str(ROOT / "nethack_harness.py"), "--dir", self.session, *args],
                                capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        if args[0] == "stop":
            return result.stdout.strip()
        return json.loads(result.stdout)

    def supervise(self, *args, payload=None):
        result = subprocess.run([sys.executable, "-m", "goal_supervisor", "--dir", self.goals, *args], cwd=ROOT,
                                input=json.dumps(payload) if payload is not None else None,
                                capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_switching_goal_changes_both_stateless_stages_and_verified_success_stops_execution(self):
        self.supervise("add", "--file", "-", payload={"id": "parent", "objective": "Inspect the area selected by the caller."})
        self.supervise("add", "--file", "-", payload=spec("left", 21, parent="parent"))
        self.supervise("activate", "left")
        self.supervise("add", "--file", "-", payload=spec("right", 23, parent="parent"))
        self.supervise("activate", "right")
        result = self.supervise("run", "--max-windows", "8")
        self.assertEqual(result["reason"], "completed")
        state = self.supervise("status")
        self.assertEqual(state["goals"]["left"]["status"], "suspended")
        self.assertEqual(state["goals"]["right"]["status"], "completed")
        self.assertEqual(state["goals"]["parent"]["status"], "pending")
        self.assertEqual(self.game.inputs, [b"\x12", b"l"])
        requests = self.endpoint.requests
        self.assertEqual([r["state"]["decision"]["stage"] for r in requests], ["tool", "arguments"])
        for request in requests:
            context = request["state"]["caller_context"]
            self.assertEqual(context["goal"]["id"], "right")
            self.assertEqual(context["parents"][0]["id"], "parent")
            self.assertEqual(context["goal"]["success"][-1]["value"], [4, 23])
        self.supervise("add", "--file", "-", payload=spec("already_there", 23))
        self.assertEqual(self.supervise("activate", "already_there")["reason"], "completed")
        self.assertEqual(self.game.inputs, [b"\x12", b"l"])
        exported = subprocess.run([sys.executable, str(ROOT / "nethack_harness.py"), "--dir", self.session, "export"],
                                  capture_output=True, text=True, timeout=40).stdout.splitlines()
        goals = [record["intent"] for record in map(json.loads, exported)
                 if record["type"] == "intent" and record["intent"]["goal_id"]]
        self.assertEqual([(g["goal_id"], g["transition"]) for g in goals], [
            ("parent", "added"), ("left", "added"), ("left", "activated"), ("right", "added"),
            ("left", "suspended"), ("right", "activated"), ("right", "completed"),
            ("already_there", "added"), ("already_there", "activated"), ("already_there", "completed")])
        self.assertEqual(goals[1]["parent_id"], "parent")
        self.assertEqual(goals[-1]["reason"], "observation_conditions")

    def test_budget_review_then_llm_revision_keeps_progress_across_cli_invocations(self):
        self.supervise("add", "--file", "-", payload=spec("destination", 25, attempts=1))
        self.supervise("activate", "destination")
        result = self.supervise("run", "--max-windows", "8")
        self.assertEqual(result["boundary"], "goal_budget_exhausted")
        self.assertEqual(self.game.inputs, [b"\x12", b"l"])
        self.supervise("update", "destination", "--file", "-", payload={"limits": {"action_attempts": 3}})
        self.supervise("activate", "destination")
        self.assertEqual(self.supervise("run", "--max-windows", "8")["reason"], "completed")
        state = self.supervise("status")
        self.assertEqual(state["goals"]["destination"]["attempts_used"], 3)
        self.assertEqual(state["goals"]["destination"]["revision"], 2)
        self.assertEqual(self.game.inputs, [b"\x12", b"l", b"l", b"l"])
        requests = [r for r in self.endpoint.requests if r["state"]["decision"]["stage"] == "arguments"]
        self.assertEqual([r["state"]["caller_context"]["goal"]["attempts_used"] for r in requests], [0, 1, 2])
        progress = [r["state"]["observation"]["objective_progress"] for r in requests]
        self.assertNotEqual(progress[0]["id"], progress[1]["id"])
        self.assertEqual(progress[1]["id"], progress[2]["id"])
        self.assertEqual([(p["scope_attempts"], p["attempts_used"], p["attempts_remaining"]) for p in progress],
                         [(0, 0, 1), (0, 0, 1), (1, 0, 1)])

    def test_windows_of_one_activation_share_the_harness_repetition_evidence(self):
        self.supervise("add", "--file", "-", payload=spec("beyond", 30, attempts=10))
        self.supervise("activate", "beyond")
        result = self.supervise("run", "--max-windows", "10")
        self.assertEqual((result["reason"], result["outcome"]["boundary"]), ("review", "repeated_no_effect"))
        self.assertEqual(self.game.inputs, [b"\x12", b"l", b"l", b"l", b"l"])
        self.supervise("activate", "beyond")
        self.supervise("run", "--max-windows", "1")
        self.assertEqual(self.game.inputs[-2:], [b"l", b"l"])

    def test_recovery_after_reservation_before_dispatch_pauses_without_sending_the_action(self):
        self.supervise("add", "--file", "-", payload=spec("destination", 23))
        self.supervise("activate", "destination")
        repository = Repository(self.goals)
        with repository.locked():
            document = repository.load()
            board = GoalBoard(document["board"])
            board.begin(self.harness("observe"), self.harness("status")["last"]["decision"])
            document["board"] = board.snapshot()
            repository.save(document)
        result = self.supervise("recover")
        self.assertEqual(result["reason"], "review")
        state = self.supervise("status")
        self.assertIsNone(state["inflight"])
        self.assertEqual(state["goals"]["destination"]["status"], "suspended")
        self.assertEqual(state["goals"]["destination"]["attempts_used"], 0)
        self.assertEqual(self.game.inputs, [b"\x12"])
