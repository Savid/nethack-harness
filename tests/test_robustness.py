"""Regression tests for failure modes found by adversarial play: door ping-pong, Mines re-entry, engulfing,
unknown prompts, bad settings, misbehaving plugins and a stuck or missing inner loop."""
import contextlib
import io
import json
import os
import tempfile
import time
import unittest

from helpers import facts, nh, screen, view
from test_policy import FakeTerm

K = nh.knowledge


class SettingsRobustnessTest(unittest.TestCase):
    def tearDown(self):
        nh.CFG.update(nh.settings.DEFAULTS)

    def test_mode_keeps_settings_it_does_not_own(self):
        nh.apply_settings(None, {"mines": "allow", "avoid": "soldier ant"})
        nh.apply_settings("careful")
        self.assertEqual((nh.CFG["mines"], nh.CFG["avoid"], nh.CFG["risk"]), ("allow", "soldier ant", "low"))
        nh.apply_settings("descend")
        self.assertEqual((nh.CFG["mines"], nh.CFG["risk"], nh.CFG["danger_max"]), ("allow", "normal", 0.8))

    def test_bad_values_change_nothing(self):
        for bad in ({"avoid": "["}, {"danger_max": "nan"}, {"stall": "abc"}, {"potions": "maybe"}, {"breaker": "inf"},
                    {"mines": "sometimes"}):
            before = dict(nh.CFG)
            with self.assertRaises(ValueError):
                nh.apply_settings(None, dict(bad, risk="high"))
            self.assertEqual(nh.CFG, before)

    def test_booleans_parse_words(self):
        nh.apply_settings(None, {"potions": "off", "elbereth": "0", "probe": "yes"})
        self.assertEqual((nh.CFG["potions"], nh.CFG["elbereth"], nh.CFG["probe"]), (0, 0, 1))


class LevelMemoryTest(unittest.TestCase):
    def test_branch_mark_survives_observation(self):
        lv = nh.Level(4)
        lv.downs[(5, 6)] = "branch"
        v = view(screen("", [" ---------- ", " |.>..@...| ", " ---------- "]), (2, 6))
        lv.observe(v)          # an unused branch staircase is drawn in the default colour
        self.assertEqual(lv.downs[(2, 3)], "main")
        lv.downs[(2, 3)] = "branch"
        lv.observe(v)
        self.assertEqual(lv.downs[(2, 3)], "branch")

    def test_levels_are_kept_per_branch(self):
        p = nh.Pilot(FakeTerm(), None)
        main = p.level(5)
        p.branch = "mines"
        self.assertIsNot(p.level(5), main)
        self.assertTrue(p.level(5).mines)
        p.branch = "main"
        self.assertIs(p.level(5), main)

    def test_overview_names_the_branch(self):
        lines = ["", "The Dungeons of Doom: levels 1 to 4", "   Level 4:", "      Stairs down to The Gnomish Mines.",
                 "The Gnomish Mines:", "   Level 5: <- You are here.", "(end)"]

        class OverviewTerm(FakeTerm):
            def __init__(self):
                super().__init__()
                self.v = view(screen("", lines[1:]), (7, 6))

        p = nh.Pilot(OverviewTerm(), None)
        self.assertEqual(p.overview(5), "mines")
        self.assertEqual(p.mines_entry, 4)


class EngulfTest(unittest.TestCase):
    def test_engulf_is_remembered_from_messages(self):
        p = nh.Pilot(FakeTerm(), None)
        v = p.term.view()
        p.message("The dust vortex engulfs you!", v)
        self.assertTrue(p.engulfed)
        p.message("You get expelled!", v)
        self.assertFalse(p.engulfed)


class PromptTest(unittest.TestCase):
    def test_unknown_text_prompt_is_not_a_map(self):
        v = view(screen("To what level do you want to teleport?", [" ---- ", " |.@.| ", " ---- "]), (0, 39))
        self.assertTrue(v.asking)
        self.assertFalse(v.normal)
        self.assertIsNone(v.hero)

    def test_unknown_text_prompt_escalates_without_keys(self):
        class PromptTerm(FakeTerm):
            def settle(self, *a):
                pass

            def poll(self, *a):
                return ""

        t = PromptTerm(screen("To what level do you want to teleport?", [" ---- ", " |.@.| ", " ---- "]), (0, 39))
        p = nh.Pilot(t, None)
        p.options = p.briefed = True
        with self.assertRaises(nh.policy.Hard) as e:
            p._step()
        self.assertIn("unknown text prompt", str(e.exception))
        self.assertEqual(t.sent, [])


class DoorTest(unittest.TestCase):
    def test_shop_doors_are_never_kicked(self):
        p = nh.Pilot(FakeTerm(), None)
        v = p.term.view()
        p.message("Hello, Hero!  Welcome to Asidonhopo's general store!", v)
        lv = p.lv[3]
        c = {"lv": lv, "watch": False, "peace": [], "obst": []}
        self.assertTrue(lv.shop)
        self.assertFalse(p.kickable((2, 2), c))          # beside the doorway we were greeted in
        self.assertTrue(p.kickable((10, 40), c))          # a locked door elsewhere on the level
        c["peace"] = [{"name": "peaceful shopkeeper", "pos": (10, 42)}]
        self.assertFalse(p.kickable((10, 40), c))


class HookRobustnessTest(unittest.TestCase):
    def plugin(self, body):
        fd, path = tempfile.mkstemp(suffix=".py")
        with os.fdopen(fd, "w") as f:
            f.write("API = 1\n" + body)
        return path

    def test_hung_plugin_is_disabled(self):
        old, nh.hooks.HOOK_SECS = nh.hooks.HOOK_SECS, 0.3
        try:
            hooks = nh.Hooks()
            name = hooks.load_plugin(self.plugin("import time\ndef on_answers(f, a):\n    time.sleep(5)\n"))
            t = time.time()
            with self.assertRaises(nh.HookError) as e:
                hooks.judge(facts(), {})
            self.assertLess(time.time() - t, 2)
            self.assertIn("disabled", str(e.exception))
            self.assertEqual(hooks.judge(facts(), {}), (None, None))
            self.assertFalse(hooks.enabled(name))
        finally:
            nh.hooks.HOOK_SECS = old

    def test_failing_plugin_is_disabled_after_two_errors(self):
        hooks = nh.Hooks()
        hooks.load_plugin(self.plugin("def on_answers(f, a):\n    raise ValueError('boom')\n"))
        with self.assertRaises(nh.HookError):
            hooks.judge(facts(), {})
        with self.assertRaises(nh.HookError) as e:
            hooks.judge(facts(), {})
        self.assertIn("disabled after 2 errors", str(e.exception))
        self.assertEqual(hooks.judge(facts(), {}), (None, None))

    def test_plugin_output_goes_to_a_capped_log(self):
        old, nh.hooks.LOG_CAP = nh.hooks.LOG_CAP, 1000
        try:
            hooks = nh.Hooks()
            hooks.log_path = tempfile.mktemp()
            hooks.load_plugin(self.plugin("def on_answers(f, a):\n    print('x' * 600)\n"))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                for _ in range(5):
                    hooks.judge(facts(), {})
            self.assertEqual(out.getvalue(), "")
            self.assertLess(os.path.getsize(hooks.log_path), 2500)
        finally:
            nh.hooks.LOG_CAP = old


class ControlRobustnessTest(unittest.TestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                rc = nh.main(list(argv))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue()

    def test_wait_without_a_loop_fails_fast(self):
        d = tempfile.mkdtemp()
        t = time.time()
        rc, text = self.run_cli("--dir", d, "wait", "--timeout", "5")
        self.assertEqual(rc, 1)
        self.assertIn("no inner loop", text)
        self.assertLess(time.time() - t, 2)

    def test_stuck_loop_is_reported(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "status.json"), "w") as f:
            json.dump({"state": "running", "pid": os.getpid(), "escalation": 0, "beat": time.time() - 60,
                       "home": os.path.realpath(d)}, f)
        rc, text = self.run_cli("--dir", d, "wait", "--timeout", "5")
        self.assertEqual(rc, 1)
        self.assertIn("stuck", text)

    def test_copied_state_dir_is_not_ours(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "status.json"), "w") as f:
            json.dump({"state": "running", "pid": os.getpid(), "escalation": 0, "beat": time.time(),
                       "home": "/somewhere/else"}, f)
        rc, text = self.run_cli("--dir", d, "wait", "--timeout", "5")
        self.assertEqual(rc, 1)
        self.assertIn("not running", text)

    def test_bad_set_is_a_usage_error(self):
        d = tempfile.mkdtemp()
        for value in ("avoid=[", "stall=abc", "danger_max=nan"):
            rc, text = self.run_cli("--dir", d, "resume", "--set", value)
            self.assertEqual(rc, 64, text)


if __name__ == "__main__":
    unittest.main()


class LiveFindingsTest(unittest.TestCase):
    def tearDown(self):
        nh.CFG.update(nh.settings.DEFAULTS)

    def test_fragile_heroes_get_a_shallow_depth_cap(self):
        p = nh.Pilot(FakeTerm(), None)
        p.role = "Valkyrie"
        self.assertEqual(p.depth_cap(1, 16, 6), 2)          # few HP: XL + fragile_lead
        self.assertEqual(p.depth_cap(1, 30, 9), 2)          # poor AC
        self.assertEqual(p.depth_cap(1, 30, 4), 1 + p.lead())
        nh.apply_settings(None, {"risk": "high"})
        self.assertEqual(p.depth_cap(1, 16, 6), 3)

    def test_female_roles_and_race_words(self):
        m = K.ATTRIBUTES.search("You are a Troglodytess, a level 1 female dwarven Cavewoman.")
        self.assertEqual((K.RACE_WORDS.get(m.group(2)), K.ROLE_NAMES.get(m.group(3))), ("dwarvish", "Caveman"))

    def test_plan_items_are_checked_and_search_finishes(self):
        for bad in ("goal:fly", "goal:rest:2", "goal:search:x", "goal:travel:1,1", "hex:zz", "keys:"):
            with self.assertRaises(ValueError):
                nh.Pilot.check_plan(bad)
        nh.Pilot.check_plan("goal:search:30")
        p = nh.Pilot(FakeTerm(), None)
        p.plan.append("goal:search:10")
        c = {"lv": p.level(3), "hero": (2, 3)}
        self.assertTrue(p.run_plan(p.term.view(), c))
        self.assertEqual(list(p.plan), [])
        self.assertEqual(p.term.sent, ["10s"])

    def test_partial_inventory_read_keeps_the_pack(self):
        class InvTerm(FakeTerm):
            def __init__(self, lines):
                super().__init__(lines)

        p = nh.Pilot(InvTerm(screen("", [" Comestibles", " d - 3 food rations", " e - 2 apples", " (end)"])), None)
        p.read_inventory()
        self.assertEqual(sorted(p.inv), ["d", "e"])
        p.term = InvTerm(screen("f - 3 fortune cookies.", [" ---- ", " |.@.| "]))
        p.read_inventory()                   # the menu never opened: only a pickup message was on screen
        self.assertEqual(sorted(p.inv), ["d", "e", "f"])

    def test_terrain_names_are_not_monsters(self):
        for name in ("wall", "dark part of a room", "doorway", "unknown"):
            self.assertTrue(K.NOT_A_MONSTER.search(name), name)
        for name in ("floating eye", "water moccasin", "stone giant", "peaceful watchman"):
            self.assertFalse(K.NOT_A_MONSTER.search(name), name)


class LycanthropyTest(unittest.TestCase):
    def test_feverish_prays_when_safe(self):
        p = nh.Pilot(FakeTerm(), None)
        p.last_prayer = None
        self.assertIsNone(p.message("The werejackal bites!  You feel feverish.", p.term.view()))
        self.assertEqual(p.plan[0], "goal:pray")
        p2 = nh.Pilot(FakeTerm(), None)
        p2.last_prayer = 300           # prayed recently: not safe, so the outer loop hears about it
        self.assertTrue(p2.message("You feel feverish.", p2.term.view()))


class SettleTest(unittest.TestCase):
    """A multi-turn command redraws the status line on the way; send() must not return at the first redraw."""

    class Scripted(nh.Term):
        def __init__(self, chunks):
            self.vt, self.cursor, self.sends, self.send_time = nh.term.VT(), 0, 0, 0.0
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
            self.assertTrue(nh.transport.MULTI_TURN.match(keys), keys)
        for keys in (b"s", b"l", b"Fh", b"\x04l", b"Za.", b"#pray\\r"):
            self.assertFalse(nh.transport.MULTI_TURN.match(keys), keys)


class CrisisTest(unittest.TestCase):
    """The loop keeps a losing fight: ladder first, hand-over only when it fails."""

    def tearDown(self):
        nh.CFG.update(nh.settings.DEFAULTS)

    def pilot(self, rows, cursor):
        p = nh.Pilot(FakeTerm(screen("", rows), cursor), None)
        p.species[(3, "d", "gray", False)] = p.species[(3, (3, 5), "d", "gray")] = "jackal"
        p.options = p.briefed = True
        p.inv_turn = 400
        return p

    def test_retreat_steps_away_and_prefers_stairs(self):
        rows = [" ------- ", " |.....| ", " |..@d.| ", " |.....| ", " ------- "]
        p = self.pilot(rows, (3, 4))
        c = p.context(p.term.view())
        a = p.retreat_act(p.term.view(), c)
        self.assertEqual(a.kind, "retreat")
        self.assertGreater(cheb_(a.target, (3, 5)), 1)
        p.level(3).up = (2, 2)
        c = p.context(p.term.view())
        a = p.retreat_act(p.term.view(), c)
        self.assertTrue(a.keys.endswith("<"), a.desc)

    def test_ladder_order_and_elbereth_rules(self):
        rows = [" ------- ", " |.....| ", " |..@d.| ", " |.....| ", " ------- "]
        p = self.pilot(rows, (3, 4))
        c = p.context(p.term.view())
        c["can_pray"], c["trouble"] = True, True
        p.crisis = {"until": 999, "dl": 3, "hp": 12, "tried": [], "why": "test"}
        keys = [a.key for a in p.crisis_ladder(p.term.view(), c, [])]
        self.assertEqual(keys[:3], ["pray", "elbereth", "retreat"])
        c["ranged"] = True                        # no Elbereth against a ranged attacker
        self.assertNotIn("elbereth", [a.key for a in p.crisis_ladder(p.term.view(), c, [])])

    def test_elbereth_is_read_back_and_retried_once(self):
        p = self.pilot([" |..@..| "], (1, 4))
        c = {"dl": 3, "hero": (1, 4), "turn": 50}
        for reads, ok in ((["Elbcreth", "Elbereth"], True), (["Elbere?h", "Elb?reth"], False)):
            flows, texts = [], list(reads)
            p.flow = lambda keys, until=8: flows.append(keys)
            p.read_engraving = lambda: texts.pop(0)
            self.assertEqual(p.engrave_elbereth(c), ok)
            self.assertEqual(len(flows), 2)
            self.assertEqual(p.elbereth_at is not None, ok)

    def test_hit_on_elbereth_means_it_failed(self):
        p = self.pilot([" |..@..| "], (1, 4))
        p.elbereth_at = (3, (1, 4), 390)
        p.hit_turn = 399
        c = p.context(p.term.view())
        self.assertFalse(c["on_elbereth"])
        self.assertEqual(p.elbereth_failed[:2], (3, (1, 4)))

    def test_handoff_setting(self):
        self.assertRaises(ValueError, nh.apply_settings, None, {"fight_handoff": "maybe"})
        nh.apply_settings(None, {"fight_handoff": "escalate"})
        self.assertEqual(nh.CFG["fight_handoff"], "escalate")

    def test_crisis_plan_items_are_checked(self):
        for good in ("goal:elbereth", "goal:quaff", "goal:quaff:f", "goal:retreat", "goal:fight:h", "goal:fight:y:6"):
            nh.Pilot.check_plan(good)
        for bad in ("goal:quaff:12", "goal:fight", "goal:fight:x", "goal:fight:h:99"):
            with self.assertRaises(ValueError):
                nh.Pilot.check_plan(bad)

    def test_rest_plan_stops_on_a_hit(self):
        p = self.pilot([" |..@..| "], (1, 4))
        p.plan.append("goal:rest:0.9")
        c = {"lv": p.level(3), "hero": (1, 4), "hpf": 0.5, "hostiles": [], "hit": True, "threats": []}
        self.assertFalse(p.run_plan(p.term.view(), c))
        self.assertEqual(list(p.plan), [])


def cheb_(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))
