import unittest

from helpers import Case, Endpoint, FakeGame, facts, nh, screen, view  # noqa: F401


class VTTest(Case):
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


if __name__ == "__main__":
    unittest.main()
