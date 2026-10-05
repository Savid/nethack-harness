from unittest import TestCase
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from unittest import mock

from helpers import Endpoint, FakeGame
from nethack_harness import __version__, control
from nethack_harness.store import Store

ROOT = Path(__file__).resolve().parent.parent
SCREEN = (b"\x1b[H\x1b[2J\x1b[3;20H------\x1b[4;20H|.@..|\x1b[5;20H------"
          b"\x1b[23;1HHero the Stripling   St:16 Dx:12 Co:14 In:9 Wi:10 Ch:8 Lawful"
          b"\x1b[24;1HDlvl:1 $:0 HP:12(16) Pw:2(2) AC:6 Xp:1 T:10\x1b[4;22H")


def cli(directory, *args):
    return subprocess.run([sys.executable, str(ROOT / "nethack_harness.py"), "--dir", directory] + list(args),
                          capture_output=True, text=True, timeout=40)


class CliTest(TestCase):
    def test_help_has_no_filesystem_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            state = os.path.join(directory, "session")
            result = cli(state, "help")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("observe", result.stdout)
            self.assertFalse(os.path.exists(state))

    def test_invalid_settings_are_rejected_before_creating_session(self):
        with tempfile.TemporaryDirectory() as directory:
            state = os.path.join(directory, "session")
            result = cli(state, "start", "--socket", "example", "--decide", "http://localhost/choose", "--max-action-steps", "0")
            self.assertEqual(result.returncode, 64)
            self.assertFalse(os.path.exists(state))

    def test_held_startup_lock_preserves_session_data(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.write("config", {"socket": "original"})
            store.write("status", {"state": "starting"})
            sequence = store.enqueue("pause")
            try:
                with open(store.path("daemon.lock"), "a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    result = cli(directory, "start", "--socket", "replacement", "--decide", "http://localhost/choose")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(store.read("config"), {"socket": "original"})
                self.assertEqual(store.read("status"), {"state": "starting"})
                self.assertEqual(store.pending(), [(sequence, {"command": "pause"})])
            finally:
                store.close()

    def test_lock_is_retained_across_daemon_handoff(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as cleanup:
            def spawn(store, lock):
                inherited = os.dup(lock.fileno())
                cleanup.callback(os.close, inherited)
                with open(store.path("daemon.lock"), "a") as competing:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
                store.write("status", {"state": "starting"})

            with mock.patch.object(control, "spawn", side_effect=spawn), mock.patch.object(control, "wait", return_value=2):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = control.main(["--dir", directory, "start", "--socket", "socket", "--decide", "http://localhost/choose"])
            self.assertEqual(result, 2)
            with open(os.path.join(directory, "daemon.lock"), "a") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_zipapp_is_reproducible_and_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [os.path.join(directory, name) for name in ("a.pyz", "b.pyz")]
            for path in paths:
                result = subprocess.run([sys.executable, str(ROOT / "tools/build_pyz.py"), "--commit", "abc123", "--out", path],
                                        capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(Path(paths[0]).read_bytes(), Path(paths[1]).read_bytes())
            result = subprocess.run([sys.executable, paths[0], "--version"], cwd=directory, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.stdout.strip(), "%s (commit abc123)" % __version__)
            game = FakeGame([SCREEN] * 10)
            endpoint = Endpoint(lambda _: {"answers": {"action": {"choice": "pause"}}})
            state = os.path.join(directory, "session")

            def packaged(*args):
                return subprocess.run([sys.executable, paths[0], "--dir", state] + list(args),
                                      cwd=directory, capture_output=True, text=True, timeout=20)

            try:
                result = packaged("start", "--socket", game.path, "--decide", endpoint.url, "--timeout", "5")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout)["status"]["reason"], "requested_pause")
                records = [json.loads(line) for line in packaged("export").stdout.splitlines()]
                self.assertEqual(records[-1]["choice"], "pause")
            finally:
                packaged("stop")
                for _ in range(50):
                    if not json.loads(packaged("status").stdout).get("alive"):
                        break
                    time.sleep(0.05)
                endpoint.close()
                game.close()


class DaemonTest(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = self.directory.name
        self.game = FakeGame([SCREEN] * 1000)
        self.endpoint = Endpoint(lambda _: {"answers": {"action": {"choice": "pause"}}})

    def tearDown(self):
        cli(self.state, "stop")
        for _ in range(50):
            if not json.loads(cli(self.state, "status").stdout).get("alive"):
                break
            time.sleep(0.05)
        self.endpoint.close()
        self.game.close()
        self.directory.cleanup()

    def start(self, *extra):
        result = cli(self.state, "start", "--socket", self.game.path, "--decide", self.endpoint.url,
                     "--quiet", "0.02", "--timeout", "5", *extra)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_review_budget_pauses_daemon_and_can_be_replaced_on_resume(self):
        self.endpoint.close()
        self.endpoint = Endpoint(lambda request: {"answers": {"action": {"choice":
                                 "search" if request["state"]["decision"]["stage"] == "tool" else "search:1"}}})
        result = self.start("--review-after-calls", "2")
        state = json.loads(result.stdout)
        self.assertEqual(state["status"]["reason"], "review_budget")
        self.assertEqual(state["status"]["calls"], 2)
        result = cli(self.state, "resume", "--timeout", "5")
        state = json.loads(result.stdout)
        self.assertEqual(state["status"]["reason"], "review_budget")
        self.assertEqual(state["status"]["calls"], 4)
        self.assertEqual(self.endpoint.requests[2]["state"]["observation"]["review_budget"]["calls_used"], 0)
        result = cli(self.state, "resume", "--review-after-calls", "1", "--timeout", "5")
        state = json.loads(result.stdout)
        self.assertEqual(state["status"]["reason"], "review_budget")
        self.assertEqual(state["status"]["calls"], 5)
        self.assertEqual(state["status"]["last"]["outcome"]["steps"], 0)
        self.assertEqual(state["observation"]["review_budget"]["limit_calls"], 1)

    def test_action_attempt_budget_is_renewed_replaced_and_disabled_on_resume(self):
        self.endpoint.close()

        def choose(request):
            state = request["state"]
            tool = "wait" if state["observation"]["objective_progress"]["attempts_used"] else "search"
            choice = tool if state["decision"]["stage"] == "tool" else tool + ":1"
            return {"answers": {"action": {"choice": choice}}}

        self.endpoint = Endpoint(choose)
        state = json.loads(self.start("--max-action-attempts", "1").stdout)
        self.assertEqual(state["status"]["reason"], "action_budget")
        initial = state["observation"]["objective_progress"]
        self.assertEqual(state["status"]["calls"], 2)
        result = cli(self.state, "resume", "--timeout", "5")
        state = json.loads(result.stdout)
        self.assertEqual(state["status"]["reason"], "action_budget")
        self.assertEqual(state["status"]["calls"], 4)
        renewed = state["observation"]["objective_progress"]
        self.assertNotEqual(initial["id"], renewed["id"])
        self.assertEqual(renewed["attempts_used"], 1)
        result = cli(self.state, "resume", "--max-action-attempts", "2", "--timeout", "5")
        state = json.loads(result.stdout)
        self.assertEqual(state["status"]["reason"], "action_budget")
        self.assertEqual(state["status"]["calls"], 8)
        self.assertEqual(state["observation"]["objective_progress"]["attempts_used"], 2)
        result = cli(self.state, "resume", "--max-action-attempts", "0", "--review-after-calls", "1", "--timeout", "5")
        state = json.loads(result.stdout)
        self.assertEqual(state["status"]["reason"], "review_budget")
        self.assertIsNone(state["observation"]["objective_progress"]["attempts_remaining"])

    def test_pause_manual_action_resume_export_and_stop(self):
        result = self.start()
        self.assertEqual(json.loads(result.stdout)["status"]["reason"], "requested_pause")
        result = cli(self.state, "act", "search:1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"s", self.game.inputs)
        result = cli(self.state, "resume", "--objective", "Explore the dungeon", "--timeout", "5")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.endpoint.requests[-1]["state"]["objective"], "Explore the dungeon")
        result = cli(self.state, "export")
        records = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([row["source"] for row in records], ["protocol", "engine", "manual", "engine"])
        self.assertEqual(records[0]["inputs"], [{"keys": "\x12", "source": "protocol", "status": "completed"}])
        self.assertEqual(cli(self.state, "stop").returncode, 0)
        store = Store(self.state)
        try:
            self.assertEqual(store.pending(), [])
        finally:
            store.close()

    def test_daemon_survives_terminated_start_client(self):
        client = subprocess.Popen([sys.executable, str(ROOT / 'nethack_harness.py'), '--dir', self.state,
                                   'start', '--socket', self.game.path, '--decide', self.endpoint.url,
                                   '--timeout', '30'], start_new_session=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for _ in range(100):
                status = json.loads(cli(self.state, 'status').stdout)
                if status.get('alive'):
                    break
                time.sleep(0.05)
            if client.poll() is None:
                os.killpg(client.pid, signal.SIGTERM)
            client.communicate(timeout=5)
            status = json.loads(cli(self.state, 'status').stdout)
            self.assertTrue(status['alive'])
            self.assertEqual(cli(self.state, 'wait', '--timeout', '5').returncode, 0)
            result = cli(self.state, 'start', '--socket', self.game.path, '--decide', self.endpoint.url)
            self.assertEqual(result.returncode, 1)
        finally:
            if client.poll() is None:
                client.kill()
            client.communicate(timeout=5)
