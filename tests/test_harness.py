"""Standard-library tests: terminal emulation, screen parsing, paths, the socket protocol, the decision
client and both hook styles. Run: python3 -m unittest discover -s tests"""
import base64
import json
import os
import socketserver
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import nethack_harness as nh  # noqa: E402


def screen(top="", rows=None, status1="Hero the Stripling   St:16 Dx:12 Co:14 In:9 Wi:10 Ch:8 Lawful",
           status2="Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:400"):
    lines = [top] + (rows or []) + [""] * 24
    lines = lines[:22] + [status1, status2]
    return [line.ljust(80)[:80] for line in lines]


def view(lines, cursor, fg=None):
    fg = fg or [["default"] * 80 for _ in range(24)]
    return nh.View(lines, fg, [[""] * 80 for _ in range(24)], cursor)


class VTTest(unittest.TestCase):
    def test_clear_position_colour(self):
        vt = nh.VT()
        vt.feed(b"\x1b[H\x1b[2Jhello\x1b[3;5H\x1b[1;33m@\x1b[0m.")
        self.assertTrue(vt.complete)
        self.assertEqual(vt.lines()[0][:5], "hello")
        self.assertEqual(vt.lines()[2][4:6], "@.")
        self.assertEqual((vt.fg[2][4], vt.bold[2][4]), ("brown", True))
        self.assertEqual((vt.fg[2][5], vt.bold[2][5]), ("default", False))
        self.assertEqual((vt.y, vt.x), (2, 6))

    def test_split_sequences_and_utf8(self):
        vt = nh.VT()
        data = "\x1b[2J\x1b[1;1Hé\x1b[7mX\x1b[27m".encode()
        for i in range(len(data)):
            vt.feed(data[i:i + 1])
        self.assertEqual(vt.lines()[0][:2], "éX")
        self.assertTrue(vt.rev[0][1])

    def test_erase_and_wrap(self):
        vt = nh.VT()
        vt.feed(b"\x1b[2J" + b"a" * 81)
        self.assertEqual(vt.lines()[1][0], "a")
        vt.feed(b"\x1b[1;3H\x1b[K")
        self.assertEqual(vt.lines()[0], "aa" + " " * 78)

    def test_scroll_region(self):
        vt = nh.VT()
        vt.feed(b"\x1b[2J\x1b[1;1Hone\r\ntwo\x1b[1;2r\x1b[2;1H\n")
        self.assertEqual(vt.lines()[0][:3], "two")


class ViewTest(unittest.TestCase):
    def test_status_and_hero(self):
        rows = [" ---- ", " |.@.| ", " ---- "]
        v = view(screen("", rows), (2, 3))
        self.assertTrue(v.normal)
        self.assertEqual(v.hero, (2, 3))
        self.assertEqual(v.st["dlvl"], 3)
        self.assertEqual((v.st["hp"], v.st["hpmax"], v.st["turn"]), (12, 16, 400))

    def test_prompts(self):
        self.assertTrue(view(screen("You hit the jackal.--More--"), (0, 30)).more)
        v = view(screen("Really attack the gnome? [yn] (n)"), (0, 30))
        self.assertEqual(v.yn, "yn")
        v = view(screen("What do you want to eat? [fg or ?*]"), (0, 30))
        self.assertEqual((v.obj, v.yn), ("fg", None))
        self.assertFalse(v.normal)


class PathTest(unittest.TestCase):
    def test_frontier_through_doorway(self):
        rows = [" ------",
                " |.@..|",
                " |....",
                " ------"]
        v = view(screen("", rows), (2, 3))
        lv = nh.Level()
        lv.observe(v)
        dist = lv.paths(v, (2, 3))
        self.assertEqual(dist[(2, 5)], 2)
        targets = [p for _, p in lv.frontier(v, dist)]
        self.assertIn((3, 5), targets)   # beside the gap in the east wall

    def test_travel_keys(self):
        self.assertEqual(nh.travel((5, 5), (6, 15)), "_@Lllj.")


class FakeGame:
    """A terminal socket that records input and replays scripted output."""

    def __init__(self, outputs, refusals=()):
        self.inputs, self.outputs, self.refusals = [], list(outputs), list(refusals)
        self.buf, self.cursor = b"", 0
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.sock")
        game = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def address_string(self):
                return "test"

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                data = base64.b64decode(req["input"])
                if data and game.refusals:
                    code, body = game.refusals.pop(0)
                    return self.reply(code, body)
                if data:
                    game.inputs.append(data)
                    if game.outputs:
                        out = game.outputs.pop(0)
                        game.buf += out
                        game.cursor += len(out)
                after = req["after"]
                start = game.cursor - len(game.buf)
                body = json.dumps({"output": base64.b64encode(game.buf[max(after, start) - start:]).decode(),
                                   "cursor": game.cursor, "truncated": after < start}).encode()
                self.reply(200, body)

            def reply(self, code, body):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self.server = Server(self.path, Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ProtocolTest(unittest.TestCase):
    def test_send_poll_and_refusals(self):
        game = FakeGame([b"\x1b[2J\x1b[1;1HHello\x1b[3;4H@"], refusals=[(503, b"busy"), (410, b"waiting for it")])
        try:
            nh.CFG["quiet"] = 0.02
            term = nh.Term(game.path)
            term.send("x")
            self.assertEqual(game.inputs, [b"x"])
            v = term.view()
            self.assertEqual(v.msg, "Hello")
            self.assertEqual(v.hero, (2, 3))
            game.refusals.append((410, b"game over"))
            with self.assertRaises(nh.Closed):
                term.send("y")
        finally:
            game.close()


class Endpoint:
    """A SystemOne-compatible stub that answers from a function and records requests."""

    def __init__(self, answer):
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append(body)
                out = json.dumps({"answers": answer(body)}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/v1/systemone" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class DecideTest(unittest.TestCase):
    def test_request_shape_and_normalised_answers(self):
        ep = Endpoint(lambda body: {"act": {"type": "choice", "choice": "b", "probabilities": {"a": 0.2, "b": 0.8}},
                                    "danger": {"type": "noul", "noul": "0.1"}})
        try:
            out = nh.normalize(nh.decider(ep.url)({"state": {"x": 1}, "questions": {"q": {"type": "noul"}}})["answers"])
            self.assertEqual(set(ep.requests[0]), {"state", "questions"})
            self.assertEqual(out["act"]["confidence"], 0.8)
            self.assertEqual(out["danger"]["noul"], 0.1)
            nh.decider(ep.url, model="m")({"state": 1, "questions": {}})
            self.assertEqual(ep.requests[1]["model"], "m")
        finally:
            ep.close()

    def test_choice_without_probabilities(self):
        out = nh.normalize({"pick": {"type": "choice", "choice": "x", "confidence": 0.7}})
        self.assertEqual(out["pick"]["probabilities"], {"x": 0.7})


def facts(decisions=1, new_level=False):
    return {"decisions": decisions, "new_level": new_level, "dlvl": 2, "hp": 10, "hpmax": 10, "screen": ""}


class HookTest(unittest.TestCase):
    def write(self, text, suffix):
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        return path

    def test_declarative_rules_gates_and_cooldown(self):
        path = self.write(json.dumps({"questions": [
            {"key": "shop", "question": {"type": "noul", "instructions": "Is a shopkeeper visible?"},
             "escalate_when": {"noul_gte": 0.8}, "cooldown": 5},
            {"key": "level", "question": {"type": "choice", "instructions": "Kind of level?",
                                          "criteria": {"normal": "ordinary", "special": "special"}},
             "escalate_when": {"choice_in": ["special"], "min_confidence": 0.6}, "when": {"new_level": True}}]}),
            ".json")
        hooks = nh.Hooks()
        self.assertEqual(hooks.load_questions(path), ["shop", "level"])
        self.assertEqual(set(hooks.due(facts(1))), {"shop"})
        self.assertEqual(set(hooks.due(facts(2, new_level=True))), {"shop", "level"})
        reason, keys = hooks.judge(facts(2), {"shop": {"noul": 0.9}})
        self.assertTrue(reason.startswith("hook:shop"))
        self.assertNotIn("shop", hooks.due(facts(4)))          # cooling down
        self.assertIn("shop", hooks.due(facts(8)))
        reason, _ = hooks.judge(facts(9), {"level": {"choice": "special", "confidence": 0.7}})
        self.assertTrue(reason.startswith("hook:level"))
        self.assertEqual(hooks.judge(facts(10), {"level": {"choice": "special", "confidence": 0.5}}), (None, None))
        hooks.disabled.add("shop")
        self.assertNotIn("shop", hooks.due(facts(30)))

    def test_bad_question_is_rejected(self):
        path = self.write(json.dumps([{"key": "x", "question": {"type": "choice"}, "escalate_when": {"noul_gte": 1}}]),
                          ".json")
        with self.assertRaises(nh.HookError):
            nh.Hooks().load_questions(path)

    def test_plugin_questions_actions_escalations_and_errors(self):
        path = self.write(
            "API = 1\n"
            "def extra_questions(facts):\n"
            "    return {'deep': {'type': 'noul', 'instructions': 'Is this level deep?'}} if facts['dlvl'] > 1 else {}\n"
            "def on_answers(facts, answers):\n"
            "    if answers.get('deep', {}).get('noul', 0) > 0.5:\n"
            "        return {'escalate': 'deep level'}\n"
            "    if facts['hp'] == 1:\n"
            "        return {'action': 's'}\n"
            "    if facts['hp'] == 0:\n"
            "        raise ValueError('boom')\n", ".py")
        hooks = nh.Hooks()
        name = hooks.load_plugin(path)
        due = hooks.due(facts())
        self.assertEqual(list(due), [name + ".deep"])
        reason, _ = hooks.judge(facts(), {name + ".deep": {"noul": 0.9}})
        self.assertEqual(reason, "hook:%s deep level" % name)
        self.assertEqual(hooks.judge(dict(facts(), hp=1), {}), (None, "s"))
        with self.assertRaises(nh.HookError):
            hooks.judge(dict(facts(), hp=0), {})


class CliTest(unittest.TestCase):
    def test_help_prints_protocol(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(nh.main(["--dir", tempfile.mkdtemp(), "help"]), 0)
        self.assertIn("OUTER/INNER LOOP PROTOCOL", out.getvalue())
        self.assertIn("resume", out.getvalue())


if __name__ == "__main__":
    unittest.main()
