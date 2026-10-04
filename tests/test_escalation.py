"""Escalation codes: every reason the loop produces maps to one registered code; pause_on and on_escalation."""
import os
import re
import tempfile

from helpers import Case, nh
from test_policy import FakeTerm

E = nh.escalation
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "nethack_harness")


class EscalationCodes(Case):
    def test_each_code_classifies_its_example(self):
        for spec in E.REGISTRY:
            self.assertEqual(E.classify(spec.example), spec.code, spec.example)

    def test_every_reason_in_the_source_has_a_code(self):
        # every literal that starts an escalation reason: Hard("..."), reason = "...", esc("..."), pause("...")
        found = []
        for name in ("policy.py", "control.py"):
            text = open(os.path.join(SRC, name)).read()
            found += re.findall(r'(?:Hard\(|reason = |self\.esc\(|pause\()\(?"([A-Za-z][^"]{3,})"', text)
        self.assertGreater(len(found), 25)
        for literal in found:
            sample = re.sub(r"%[-+ 0-9]*(?:\.\d+)?[dsf]", "1", literal)    # fill in the format fields
            self.assertNotEqual(E.classify(sample), "other", literal)

    def test_pause_on(self):
        self.assertEqual(E.parse_pause_on("all"), set(E.BY_CODE))
        on = E.parse_pause_on("all,-milestone,-branch_point,-losing_fast")
        self.assertNotIn("milestone", on)
        self.assertIn("losing_fast", on)                 # safety codes cannot be silenced
        self.assertIn("game_over", E.parse_pause_on("milestone"))
        self.assertRaises(ValueError, nh.apply_settings, None, {"pause_on": "all,-nonsense"})


class EscalationRouting(Case):
    def plugin(self, body):
        fd, path = tempfile.mkstemp(suffix=".py")
        with os.fdopen(fd, "w") as f:
            f.write("API = 1\n" + body)
        return path

    def test_silenced_codes_play_on(self):
        p = nh.Pilot(FakeTerm(), None)
        p.hooks = nh.Hooks()
        self.assertEqual(p.esc("milestone: new deepest Dlvl 3"), "milestone: new deepest Dlvl 3")
        nh.apply_settings(None, {"pause_on": "all,-milestone"})
        self.assertIsNone(p.esc("milestone: new deepest Dlvl 4"))
        self.assertTrue(p.esc("low HP 3/16 with jackal near"))

    def test_on_escalation_can_continue_or_plan(self):
        p = nh.Pilot(FakeTerm(), None)
        p.hooks = nh.Hooks()
        p.hooks.load_plugin(self.plugin(
            "def on_escalation(facts, esc):\n"
            "    if esc['code'] == 'depth_gate':\n"
            "        return {'plan': ['goal:explore:20', 'goal:bogus']}\n"
            "    if esc['code'] == 'branch_point':\n"
            "        return {'continue': True}\n"))
        self.assertIsNone(p.esc("branch point: two down staircases on Dlvl 4"))
        self.assertIsNone(p.esc("depth gate: Dlvl 2 is explored"))
        self.assertEqual(list(p.plan), ["goal:explore:20"])          # invalid items are dropped and logged
        self.assertTrue(p.esc("surrounded: 3 adjacent hostiles"))    # never offered to plugins
