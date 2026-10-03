import json
import os
import tempfile
import unittest

from helpers import Case, Endpoint, FakeGame, facts, nh, screen, view  # noqa: F401


class ProtocolTest(Case):
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


class RefusalTest(Case):
    def test_410_bodies(self):
        game = FakeGame([b"x"] * 10, refusals=[(410, b"game is stopping")])
        try:
            with self.assertRaises(nh.Closed):
                nh.Term(game.path).poll(b"k")
            game.refusals[:] = [(410, b"waiting for the race")] * 50
            with self.assertRaises(nh.transport.Held):
                nh.Term(game.path).poll(b"k", hold_wait=0.3)
            game.refusals[:] = [(400, b"invalid terminal request")]
            with self.assertRaises(RuntimeError):
                nh.Term(game.path).poll(b"k")
        finally:
            game.close()


if __name__ == "__main__":
    unittest.main()
