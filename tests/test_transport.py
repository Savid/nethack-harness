from unittest import TestCase
import time
import unittest

from helpers import FakeGame
from nethack_harness import transport
from nethack_harness.base import unescape
from nethack_harness.transport import Term, Closed, Held
from nethack_harness.term import VT
from nethack_harness.screen import PromptContext


class ProtocolTest(TestCase):
    def test_a_position_prompt_opened_inside_a_batch_is_tracked_and_bytes_are_sent_raw(self):
        def frame(message, cursor):
            return (b"\x1b[2J\x1b[1;1H" + message + b"\x1b[3;4H@.." +
                    b"\x1b[23;1HHero St:16 Lawful\x1b[24;1HDlvl:1 HP:14(14) Pw:3(3) AC:7 Xp:1 T:1" + cursor)

        initial = frame(b"", b"\x1b[3;4H")
        game = FakeGame([frame(b"Where do you want to travel?", b"\x1b[3;4H"),
                         frame(b"floor of a room", b"\x1b[3;5H"), frame(b"floor of a room", b"\x1b[3;6H"),
                         frame(b"Are you sure you want to pray? [yn] (n)", b"\x1b[1;40H")])
        try:
            game.buf, game.cursor = initial, len(initial)
            term = Term(game.path, quiet=0.02)
            term.poll()
            term.send("_ll", separately=True)
            self.assertEqual(game.inputs, [b"_", b"l", b"l"])
            self.assertTrue(term.view().getpos)
            self.assertIsNone(term.view().hero)
            term.send(unescape(r"\xf0"))
            self.assertEqual(game.inputs[-1], b"\xf0")
        finally:
            game.close()

    def test_targeting_survives_descriptions_and_refused_cancel(self):
        def frame(message, cursor):
            return (b"\x1b[2J\x1b[1;1H" + message + b"\x1b[3;4H@+" +
                    b"\x1b[24;1HDlvl:1 HP:14(14) Pw:3(3) AC:7 Xp:1 T:1" + cursor)

        initial = frame(b"Pick an object.", b"\x1b[3;4H")
        game = FakeGame([frame(b"a closed door", b"\x1b[3;5H"), frame(b"", b"\x1b[3;4H")])
        try:
            game.buf, game.cursor = initial, len(initial)
            term = Term(game.path, quiet=0.02)
            term.poll()
            self.assertTrue(term.view().getpos)
            term.send("l")
            self.assertTrue(term.view().getpos)
            self.assertIsNone(term.view().hero)
            game.refusals.append((400, b"refused"))
            with self.assertRaises(RuntimeError):
                term.send("\x1b")
            self.assertTrue(term.view().getpos)
            term.send("\x1b")
            self.assertFalse(term.view().getpos)
            self.assertEqual(term.view().hero, (2, 3))
            self.assertEqual(game.inputs, [b"l", b"\x1b"])
        finally:
            game.close()

    def test_before_send_checks_the_latest_screen_before_input(self):
        game = FakeGame([])
        try:
            term = Term(game.path)
            initial = b"\x1b[2J\x1b[1;1HInitial\x1b[3;4H@"
            game.buf, game.cursor = initial, len(initial)
            term.poll()
            update = b"\x1b[1;1HChanged"
            game.buf += update
            game.cursor += len(update)

            def reject(view):
                self.assertEqual(view.msg, "Changed")
                raise RuntimeError("screen changed")

            with self.assertRaisesRegex(RuntimeError, "screen changed"):
                term.send("l", before_send=reject)
            self.assertEqual(game.inputs, [])
        finally:
            game.close()

    def test_send_poll_and_refusals(self):
        game = FakeGame([b"\x1b[2J\x1b[1;1HHello\x1b[3;4H@"], refusals=[(503, b"busy"), (410, b"waiting for it")])
        try:
            term = Term(game.path, quiet=0.02)
            term.send("x")
            self.assertEqual(game.inputs, [b"x"])
            v = term.view()
            self.assertEqual(v.msg, "Hello")
            self.assertEqual(v.hero, (2, 3))
            game.refusals.append((410, b"game over"))
            with self.assertRaises(Closed):
                term.send("y")
        finally:
            game.close()


class RefusalTest(TestCase):
    def test_410_bodies(self):
        game = FakeGame([b"x"] * 10, refusals=[(410, b"game is stopping")])
        try:
            with self.assertRaises(Closed):
                Term(game.path).poll(b"k")
            game.refusals[:] = [(410, b"waiting for game input to be enabled")] * 50
            with self.assertRaises(Held):
                Term(game.path).poll(b"k", hold_wait=0.3)
            game.refusals[:] = [(400, b"invalid terminal request")]
            with self.assertRaises(RuntimeError):
                Term(game.path).poll(b"k")
        finally:
            game.close()


class SettleTest(TestCase):
    """A multi-turn command redraws the status line on the way; send() must not return at the first redraw."""

    class Scripted(Term):
        def __init__(self, chunks):
            self.quiet = 0.02
            self.vt, self.cursor, self.after_run = VT(), 0, False
            self.prompts = PromptContext()
            self.chunks, self.t0 = chunks, None     # (seconds after the keys, bytes)

        def poll(self, data=b"", hold_wait=2.0):
            if data:
                self.t0 = time.monotonic()
            now = time.monotonic() - self.t0 if self.t0 else 0.0
            out = b""
            while self.chunks and self.chunks[0][0] <= now:
                out += self.chunks.pop(0)[1]
            if out:
                self.vt.feed(out)
            return out

    def game(self):
        def status(t):
            return b"\x1b[24;1HDlvl:1 $:0 HP:14(14) Pw:3(3) AC:7 Xp:1 T:%d\x1b[12;40H" % t

        start = b"\x1b[2J\x1b[12;40H@" + status(1)
        return start, [(0.0, status(8)), (0.08, status(15)), (0.16, status(21))]

    def test_counted_search_waits_for_the_last_redraw(self):
        start, chunks = self.game()
        t = self.Scripted(chunks)
        t.vt.feed(start)
        t.send(b"20s")
        self.assertIn("T:21", t.view().rows[23])

    def test_multi_turn_classification(self):
        for keys in (b"20s", b"_@ll.", b"Gl", b"L", b"n20s", b"m2s"):
            self.assertTrue(transport.MULTI_TURN.match(keys), keys)
        for keys in (b"s", b"l", b"Fh", b"\x04l", b"Za.", b"#pray\\r"):
            self.assertFalse(transport.MULTI_TURN.match(keys), keys)


if __name__ == "__main__":
    unittest.main()
