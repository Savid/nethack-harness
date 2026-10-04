"""The background loop end to end against a scripted terminal socket: detach, commands, game over."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from helpers import Case, FakeGame

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SCREEN = (b"\x1b[H\x1b[2J" + b"\x1b[3;20H------" + b"\x1b[4;20H|.@..|" + b"\x1b[5;20H------" +
          b"\x1b[23;1HHero the Stripling   St:16 Dx:12 Co:14 In:9 Wi:10 Ch:8 Lawful" +
          b"\x1b[24;1HDlvl:1 $:0 HP:12(16) Pw:2(2) AC:6 Xp:1 T:10" + b"\x1b[4;22H")


class StaticGame(FakeGame):
    """Every key redraws the same small room; optionally answers 410 'game is stopping'."""

    def __init__(self):
        super().__init__([SCREEN] * 100000)


def cli(state, *args, timeout=40):
    return subprocess.run([sys.executable, os.path.join(ROOT, "nethack_harness.py"), "--dir", state] + list(args),
                          capture_output=True, text=True, timeout=timeout)


class DaemonTest(Case):
    def setUp(self):
        super().setUp()
        self.game = StaticGame()
        self.state = tempfile.mkdtemp()

    def tearDown(self):
        cli(self.state, "stop")
        time.sleep(0.5)
        self.game.close()
        super().tearDown()

    def status(self):
        return json.loads(cli(self.state, "status").stdout)

    def test_detached_loop_survives_a_killed_start_and_obeys_commands(self):
        start = subprocess.Popen([sys.executable, os.path.join(ROOT, "nethack_harness.py"), "--dir", self.state,
                                  "start", "--socket", self.game.path, "--decide", "none", "--set", "briefing=0",
                                  "--set", "quiet=0.02", "--timeout", "60"], start_new_session=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(100):
            if self.status().get("pid"):
                break
            time.sleep(0.1)
        os.killpg(start.pid, signal.SIGTERM)     # as an agent tool does to a timed-out command
        start.communicate(timeout=10)
        time.sleep(1)
        st = self.status()
        self.assertTrue(st["alive"], st)
        out = cli(self.state, "pause")
        self.assertIn(out.returncode, (0, 2))
        for _ in range(50):
            if self.status()["state"] == "paused":
                break
            time.sleep(0.1)
        self.assertEqual(self.status()["state"], "paused")
        self.assertIn("Dlvl:1", cli(self.state, "screen").stdout)
        self.assertEqual(cli(self.state, "send", "x" * 5000).returncode, 64)
        sent = cli(self.state, "send", "s")
        self.assertEqual(sent.returncode, 0, sent.stdout + sent.stderr)
        self.assertEqual(cli(self.state, "start", "--socket", self.game.path, "--decide", "none").returncode, 1)
        cli(self.state, "stop")
        for _ in range(50):
            if not self.status()["alive"]:
                break
            time.sleep(0.1)
        self.assertFalse(self.status()["alive"])

    def test_game_gone_ends_with_postmortem_and_notes(self):
        notes = os.path.join(self.state, "notes.json")
        out = cli(self.state, "start", "--socket", self.game.path, "--decide", "none", "--set", "briefing=0",
                  "--set", "quiet=0.02", "--notes-out", notes, "--timeout", "2")
        self.assertIn(out.returncode, (0, 2), out.stdout + out.stderr)
        self.game.close()
        os.unlink(self.game.path)                 # the game's terminal goes away
        for _ in range(100):
            if self.status().get("state") == "ended":
                break
            time.sleep(0.1)
        self.assertEqual(self.status().get("state"), "ended")
        self.assertTrue(os.path.exists(os.path.join(self.state, "postmortem.txt")))
        self.assertTrue(os.path.exists(notes))
        log = open(os.path.join(self.state, "daemon.log")).read()
        self.assertNotIn("Traceback", log)


if __name__ == "__main__":
    unittest.main()
