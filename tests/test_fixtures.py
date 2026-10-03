"""Golden screens: a fresh pilot's top actions on recorded and crafted screens (no game, no I/O).

When a rule change moves these on purpose, run `python3 tools/record_fixtures.py regen tests/fixtures/screens`
and review the diff: it is the behaviour change."""
import os

from helpers import Case, nh
import fixturefmt

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "screens")


class GoldenScreens(Case):
    def test_rankings_match(self):
        names = sorted(n for n in os.listdir(DIR) if n.endswith(".screen"))
        self.assertGreaterEqual(len(names), 40)
        for name in names:
            args, lookups, expect = fixturefmt.load(open(os.path.join(DIR, name)).read())
            keys, sent = fixturefmt.fresh_ranking(nh, args, lookups)
            self.assertEqual(keys, expect, name)
            self.assertEqual(sent, [], name + ": ranking a screen must not send keys")

    def test_round_trip(self):
        for name in sorted(os.listdir(DIR))[:10]:
            text = open(os.path.join(DIR, name)).read()
            args, lookups, expect = fixturefmt.load(text)
            self.assertEqual(fixturefmt.dump(nh.View(*args), lookups.items(), expect), text, name)

    def test_safety_on_crafted_screens(self):
        def top(name):
            args, lookups, _ = fixturefmt.load(open(os.path.join(DIR, name + ".screen")).read())
            return fixturefmt.fresh_ranking(nh, args, lookups, top=10)[0]
        self.assertFalse([k for k in top("craft-gas-spore-adjacent") if k.startswith(("attack", "fire", "throw"))])
        self.assertFalse([k for k in top("craft-nymph") if k.startswith("attack")])
        self.assertEqual(top("craft-jackal")[0], "attack_l")
        self.assertEqual(top("craft-closed-door")[0], "open_l")
