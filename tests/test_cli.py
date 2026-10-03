import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest

from helpers import nh

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


class CliTest(unittest.TestCase):
    def test_help_is_a_full_map_and_creates_nothing(self):
        d = tempfile.mkdtemp()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(nh.main(["--dir", os.path.join(d, "state"), "help"]), 0)
        text = out.getvalue()
        for word in ("PROTOCOL", "COMMANDS", "SETTINGS", "EFFORT", "PLAN QUEUE", "HOOKS", "PLUGINS", "PLAYBOOK"):
            self.assertIn(word, text)
        self.assertFalse(os.path.exists(os.path.join(d, "state")))

    def test_help_topic(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            nh.main(["help", "effort"])
        self.assertIn("off", out.getvalue())

    def test_entry_points(self):
        for cmd in ([sys.executable, os.path.join(ROOT, "nethack_harness.py"), "--version"],
                    [sys.executable, "-m", "nethack_harness", "--version"]):
            out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn(nh.__version__, out.stdout)

    def test_zipapp_is_reproducible_and_runs(self):
        d = tempfile.mkdtemp()
        outs = []
        for name in ("a.pyz", "b.pyz"):
            out = os.path.join(d, name)
            subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build_pyz.py"), "--commit", "abc123",
                            "--out", out], check=True, capture_output=True, timeout=60)
            with open(out, "rb") as f:
                outs.append(f.read())
        self.assertEqual(outs[0], outs[1])
        run = subprocess.run([sys.executable, os.path.join(d, "a.pyz"), "--version"], capture_output=True,
                             text=True, timeout=30, cwd=d)
        self.assertEqual(run.stdout.strip(), "%s (commit abc123)" % nh.__version__)


if __name__ == "__main__":
    unittest.main()
