"""Regression tests for failure modes found by adversarial play: door ping-pong, Mines re-entry, engulfing,
unknown prompts, bad settings, misbehaving plugins and a stuck or missing inner loop."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from helpers import Case, facts, nh, screen, view
from test_policy import FakeTerm

K = nh.knowledge


class SettingsRobustnessTest(Case):
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


class LevelMemoryTest(Case):
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


class EngulfTest(Case):
    def test_engulf_is_remembered_from_messages(self):
        p = nh.Pilot(FakeTerm(), None)
        v = p.term.view()
        p.message("The dust vortex engulfs you!", v)
        self.assertTrue(p.engulfed)
        p.message("You get expelled!", v)
        self.assertFalse(p.engulfed)


class PromptTest(Case):
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


class DoorTest(Case):
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


class HookRobustnessTest(Case):
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


class ControlRobustnessTest(Case):
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
        fake = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "_daemon"])
        try:
            with open(os.path.join(d, "status.json"), "w") as f:
                json.dump({"state": "running", "pid": fake.pid, "escalation": 0, "beat": time.time() - 60,
                           "home": os.path.realpath(d)}, f)
            rc, text = self.run_cli("--dir", d, "wait", "--timeout", "5")
            self.assertEqual(rc, 1)
            self.assertIn("stuck", text)
            # nobody holds the dir's lock, so stop must not signal that process
            rc, text = self.run_cli("--dir", d, "stop")
            self.assertIn("marked stopped", text)
            self.assertIsNone(fake.poll())
        finally:
            fake.kill()

    def test_a_reused_pid_is_not_a_daemon(self):
        stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            st = {"state": "running", "pid": stranger.pid}
            self.assertFalse(nh.control.alive(st))
        finally:
            stranger.kill()

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


class LiveFindingsTest(Case):
    def test_pace_and_fragility(self):
        p = nh.Pilot(FakeTerm(), None)
        p.role = "Valkyrie"                                   # role lead 4
        self.assertFalse(p.fragile(16, 6, 1))                 # a Valkyrie start is sturdy
        self.assertTrue(p.fragile(12, 9, 1))                  # a Tourist-like start is fragile
        self.assertEqual(p.depth_cap(1, 16, 6), 2)            # XL+1 until XL 4...
        self.assertEqual(p.depth_cap(4, 40, 4), 6)            # ...then XL+2 when sturdy
        self.assertEqual(p.depth_cap(4, 20, 9), 5)            # a fragile hero keeps XL+1
        cap, binds, how = p.depth_limits(1, 16, 6)
        self.assertEqual(binds, "pace")
        self.assertIn("fragile_lead", how)
        nh.apply_settings(None, {"risk": "low"})
        p.role = "Healer"                                     # lead 2 - 1 = 1 binds with fragile_lead 2
        nh.apply_settings(None, {"fragile_lead": "2"})
        cap, binds, how = p.depth_limits(1, 12, 9)
        self.assertEqual((cap, binds), (2, "lead"))
        self.assertIn("--set lead=N", how)

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
        for name in ("wall", "dark part of a room", "doorway", "unknown", "a doorway or the floor of a room (r)"):
            self.assertTrue(K.not_a_monster(name), name)
        for name in ("floating eye", "water moccasin", "stone giant", "peaceful watchman"):
            self.assertFalse(K.not_a_monster(name), name)


class LycanthropyTest(Case):
    def test_feverish_escalates_with_the_remedy(self):
        p = nh.Pilot(FakeTerm(), None)
        p.hooks = nh.Hooks()
        reason = p.message("The werejackal bites!  You feel feverish.", p.term.view())
        self.assertTrue(reason.startswith("alarming message:"))
        self.assertIn("goal:pray", reason)
        self.assertEqual(list(p.plan), [])          # the outer loop (or a plugin) decides


class SettleTest(Case):
    """A multi-turn command redraws the status line on the way; send() must not return at the first redraw."""

    class Scripted(nh.Term):
        def __init__(self, chunks):
            self.vt, self.cursor, self.sends, self.send_time, self.seen = nh.term.VT(), 0, 0, 0.0, ""
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


class CrisisTest(Case):
    """The loop keeps a losing fight: ladder first, hand-over only when it fails."""

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
        self.assertEqual((a.target, a.keys.startswith("_")), ((2, 2), True), a.desc)

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


class MilestoneTest(Case):
    def c(self, p, dl, xl, hpf=1.0, hostiles=()):
        return {"dl": dl, "xl": xl, "hp": int(20 * hpf), "hpmax": 20, "hpf": hpf, "turn": 100 * dl,
                "hostiles": list(hostiles), "lv": p.level(dl)}

    def test_pauses_once_per_new_depth_when_healthy(self):
        p = nh.Pilot(FakeTerm(), None)
        nh.apply_settings(None, {"milestone": "depth"})
        p.milestone(self.c(p, 1, 1))                      # first step: no pause
        p.milestone(self.c(p, 2, 1, hpf=0.5))             # hurt: deferred
        with self.assertRaises(nh.policy.Hard) as e:
            p.milestone(self.c(p, 2, 1))
        self.assertTrue(str(e.exception).startswith("milestone: new deepest Dlvl 2 (XL 1"))
        p.milestone(self.c(p, 2, 2))                      # XL milestones are off: silent
        p.milestone(self.c(p, 1, 2))                      # going back up is not news
        self.assertEqual([m["dlvl"] for m in p.milestones], [2])

    def test_xl_milestones_and_validation(self):
        p = nh.Pilot(FakeTerm(), None)
        nh.apply_settings(None, {"milestone": "xl"})
        p.milestone(self.c(p, 1, 1))
        p.milestone(self.c(p, 2, 1))
        with self.assertRaises(nh.policy.Hard):
            p.milestone(self.c(p, 2, 2))
        self.assertRaises(ValueError, nh.apply_settings, None, {"milestone": "sometimes"})


class CodeReviewTest(Case):
    def test_help_lists_every_setting_once(self):
        import re as re_
        names = re_.findall(r"^  (\w+)", nh.control.help_text("settings"), re_.M)
        self.assertEqual(sorted(names), sorted(nh.settings.DEFAULTS))

    def test_values_are_validated(self):
        for bad in ({"mode": "banana"}, {"mapping": "3"}, {"danger_max": "1.5"}, {"stall": "-1"}, {"dig": "2"},
                    {"xl_lead": "2"}):
            with self.assertRaises(ValueError, msg=bad):
                nh.apply_settings(None, bad)
        nh.apply_settings(None, {"mapping": "2", "lead": "-1"})
        self.assertEqual((nh.CFG["mapping"], nh.CFG["lead"]), (2, -1))

    def test_play_clock_stops_while_paused(self):
        p = nh.Pilot(FakeTerm(), None)
        t0 = p.clock()
        p.stop_clock()
        time.sleep(0.3)
        self.assertLess(p.clock() - t0, 0.1)
        p.start_clock()
        time.sleep(0.1)
        self.assertGreaterEqual(p.clock() - t0, 0.09)
        self.assertLess(p.clock() - t0, 0.25)

    def test_wielded_weapons_are_never_thrown(self):
        p = nh.Pilot(FakeTerm(), None)
        p.inv = {"a": ("a +1 dwarvish spear (weapon in right hand)", "Weapons"),
                 "b": ("a +0 dagger (alternate weapon; not wielded)", "Weapons"),
                 "c": ("2 apples", "Comestibles")}
        self.assertEqual(p.spare_missile()[0], "b")          # the alternate dagger, never the wielded spear
        del p.inv["b"]
        self.assertIsNone(p.spare_missile())          # the only food is never thrown
        p.inv["d"] = ("2 food rations", "Comestibles")
        self.assertEqual(p.spare_missile()[0], "c")   # spare fruit is
        self.assertEqual(nh.Pilot.min_range({"name": "gas spore"}), 2)


class PostmortemTest(Case):
    def test_prayer_bands(self):
        p = nh.Pilot(FakeTerm(), None)
        self.assertEqual(p.prayer_band(400), "safe")
        p.last_prayer = 300
        self.assertTrue(p.prayer_band(400).startswith("fails (last T300, 100 ago)"))
        self.assertTrue(p.prayer_band(700).startswith("uncertain (last T300, 400 ago"))
        self.assertTrue(p.prayer_band(1000).startswith("likely in major trouble (last T300, 700 ago"))
        self.assertIn("1/7", p.prayer_band(1300, trouble=False))
        self.assertEqual(p.prayer_band(1300), "safe")
        p.prayer_broken = True
        self.assertTrue(p.prayer_band(5000).startswith("broken"))

    def test_postmortem_names_the_killer_and_the_ladder(self):
        p = nh.Pilot(FakeTerm(), None)
        v = p.term.view()
        for text in ("The jackal bites!", "The gnome lord zaps a wand!", "You die..."):
            p.message(text, v)
        p.hp_trail.extend([(398, 6, 16), (399, 2, 16)])
        p.last_crisis = {"why": "HP 6/16", "tried": ["elbereth", "retreat"], "ended": 399, "hp_end": 2}
        text = nh.report.postmortem(p, "game_over")
        self.assertIn("killer (best guess): gnome lord", text)
        self.assertIn("tried: elbereth, retreat", text)
        self.assertIn("399:2/16", text)
        self.assertIn("T400 You die...", text)


class PersonaTest(Case):
    def test_inventory_menu_drawn_over_the_map(self):
        rows = [" ------           Comestibles",
                " |....|           d - 9 food rations",
                " |.@..|           e - an apple",
                " |....|           Potions",
                " ------           f - a potion of extra healing",
                "                  (end)"]
        p = nh.Pilot(FakeTerm(screen("", rows), (3, 3)), None)
        p.read_inventory()
        self.assertEqual(sorted(p.inv), ["d", "e", "f"])
        self.assertEqual([t for _, t in p.items(K.HEALING.pattern)], ["a potion of extra healing"])

    def test_food_is_matched_on_whole_words(self):
        self.assertIsNone(K.food_index("a +1 spear (weapon in right hand)"))
        self.assertIsNotNone(K.food_index("2 pears"))
        self.assertIsNotNone(K.food_index("an uncursed food ration"))


class PersonaP1Test(Case):
    def test_send_escapes_and_one_based_positions(self):
        self.assertEqual(nh.control.unescape(r"#pray\r"), "#pray\r")
        self.assertEqual(nh.control.unescape(r"\e\x04l"), "\x1b\x04l")
        self.assertEqual(nh.level.pos1((5, 50)), "6,51")

    def test_spells_are_learned_and_used_by_cost(self):
        p = nh.Pilot(FakeTerm(), None)
        p.read_pages = lambda keys: ["   a - force bolt             1   attack         0%      100%",
                                     "   b - healing                1   healing        0%      100%"]
        p.learn_spells()
        v = p.term.view()                          # Pw 2 on the test status line
        self.assertIsNone(p.spell("attack", v))
        v.st["pw"] = 10
        self.assertEqual(p.spell("attack", v), ("a", "force bolt"))
        self.assertEqual(p.spell("heal", v), ("b", "healing"))


class AdversarialR3Test(Case):
    def test_fights_and_kicks_are_not_oscillation(self):
        for key in ("attack_l", "kick_k"):
            p = nh.Pilot(FakeTerm(), None)
            p.decisions, p.progress = 40, 30
            for _ in range(10):
                p.trail.append((3, (10, 40), key))
            self.assertIsNone(p.oscillation(3))
            self.assertIsNone(p.level(3).bans.get(((10, 40), key)))

    def test_shop_sounds_do_not_make_a_shop_door(self):
        p = nh.Pilot(FakeTerm(), None)
        p.message("You hear someone cursing shoplifters.", p.term.view())
        lv = p.lv[3]
        self.assertTrue(lv.has_shop)
        self.assertFalse(lv.shop or lv.no_dig or lv.shop_doors)

    def test_kills_count_as_progress(self):
        p = nh.Pilot(FakeTerm(), None)
        p.decisions, p.progress = 50, 10
        p.message("You kill the imp!", p.term.view())
        self.assertEqual(p.progress, 50)

    def test_repeat_refuses_a_bare_count(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                rc = nh.main(["--dir", tempfile.mkdtemp(), "repeat", "20", "--times", "2"])
            except SystemExit as e:
                rc = e.code
        self.assertEqual(rc, 64)

    def test_terrain_lists_are_not_monsters(self):
        self.assertTrue(K.not_a_monster("a doorway or the floor of a room or the dark part of a room (r)"))
        self.assertFalse(K.not_a_monster("imp (peaceful imp)"))


class JournalTest(Case):
    def test_keys_are_journaled_and_replayable(self):
        d = tempfile.mkdtemp()
        p = nh.Pilot(FakeTerm(), None)
        p.journal = open(os.path.join(d, "keys.jsonl"), "a")
        p.last_st = {"turn": 5, "dlvl": 1}
        p.send("Fh")
        p.record("#pray\r", "hand")
        p.journal.close()
        store = nh.control.Store(d)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            nh.control.print_keys(store, raw=True)
        self.assertEqual(out.getvalue().split(), ["Fh", "#pray\\r"])
        path = os.path.join(d, "replay.txt")
        with open(path, "w") as f:
            f.write(out.getvalue())
        nh.Pilot.check_plan("replay:" + path)
        q = nh.Pilot(FakeTerm(), None)
        q.plan.append("replay:" + path)
        c = {"hp": 10, "hpmax": 10}
        self.assertTrue(q.run_plan(q.term.view(), c))
        self.assertTrue(q.run_plan(q.term.view(), c))
        self.assertFalse(q.run_plan(q.term.view(), c))
        self.assertEqual(q.term.sent, ["Fh", "#pray\r"])

    def test_tiebreak_seed_is_a_setting(self):
        nh.apply_settings(None, {"tiebreak_seed": "7"})
        self.assertEqual(nh.CFG["tiebreak_seed"], 7)


class NotesTest(Case):
    def test_export_merge_round_trip_and_anchor(self):
        a = nh.Pilot(FakeTerm(), None)
        a.anchor = "abc"
        lv = a.level(5)
        lv.downs[(11, 44)] = "main"
        lv.downs[(3, 3)] = "branch"
        lv.up = (4, 9)
        lv.traps[(7, 29)] = "trap door"
        data = json.loads(json.dumps(nh.notes.export(a)))
        self.assertEqual(data["levels"]["main:5"]["down"], [[12, 45]])
        b = nh.Pilot(FakeTerm(), None)
        b.anchor = "other"
        self.assertIn("another game", nh.notes.merge(b, data))
        b.anchor = "abc"
        self.assertEqual(nh.notes.merge(b, data), ["main Dlvl 5"])
        lb = b.level(5)
        self.assertEqual((lb.imported["down"], lb.up, lb.downs.get((3, 3)), lb.traps.get((7, 29))),
                         ([(11, 44)], (4, 9), "branch", "trap door"))
        self.assertIn("down 12,45 [imported]", nh.notes.lines(b))


class EndgameTest(Case):
    def test_time_left_lifts_caps_and_prefers_descent(self):
        p = nh.Pilot(FakeTerm(), None)
        p.role = "Tourist"
        self.assertIsNone(p.seconds_left())
        self.assertEqual(p.depth_cap(1, 10, 9), 2)
        self.assertEqual(p.mines_policy(), "avoid")
        nh.apply_settings(None, {"time_left": "100", "endgame_secs": "180"})
        p.set_time_left(nh.CFG["time_left"])
        self.assertTrue(p.endgame())
        self.assertEqual(p.depth_cap(1, 10, 9), 99)
        self.assertEqual(p.mines_policy(), "allow")
        c = {"lv": p.level(3), "xl": 1, "hpmax": 10, "ac": 9, "dl": 6, "hpf": 0.55}
        self.assertTrue(p.descend_ok(c))
        p.set_time_left(1000)                     # more time: no endgame, the caps return
        self.assertFalse(p.endgame())
        self.assertFalse(p.descend_ok(c))
        self.assertEqual(nh.escalation.classify("endgame: 170 s left"), "endgame")
        self.assertRaises(ValueError, nh.apply_settings, None, {"deadline": "1"})

    def test_time_left_is_unknown_in_another_process(self):
        p = nh.Pilot(FakeTerm(), None)
        p.set_time_left(500)
        self.assertGreater(p.seconds_left(), 490)
        p.time_budget = (500.0, p.time_budget[1], "another-boot/1")   # what a copied or restarted state holds
        self.assertIsNone(p.seconds_left())
        self.assertFalse(p.endgame())
        self.assertIn("unknown", nh.report.time_left_text(p))


class CompactOutputTest(Case):
    def test_compact_report_is_small_and_help_brief_fits(self):
        import random as random_
        import fixturefmt
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "screens")
        for name in sorted(os.listdir(d))[:15]:
            args, lookups, _ = fixturefmt.load(open(os.path.join(d, name)).read())
            term = fixturefmt.ScreenTerm(nh.View, args)
            p = nh.Pilot(term, None)
            p.options = p.briefed = True
            p.inv_turn, p.rng, p.lookup = 10 ** 9, random_.Random(0), (lambda pos, lk=lookups: lk.get(pos))
            v = term.view()
            if not v.normal:
                continue
            p.branch_dl = v.st.get("dlvl")
            p.view()
            p.actions(v, p.context(v))
            text = nh.report.summary(p, "low HP 4/16 with jackal near and no safe prayer, potion or Elbereth")
            self.assertLess(len(text), 1200, name)
            self.assertIn("ESCALATION [low_hp]", text)
        self.assertLessEqual(len(nh.control.help_text("brief").encode()), 2500)


class V7Test(Case):
    def pilot(self, rows, cursor, lookups):
        p = nh.Pilot(FakeTerm(screen("", rows), cursor), None)
        p.lookup = lambda pos: lookups.get(pos)
        p.options = p.briefed = True
        p.inv_turn = 10 ** 9
        return p

    def test_no_keys_into_a_dead_game(self):
        p = nh.Pilot(FakeTerm(screen("You die...--More--", [" |..@..| "]), (0, 18)), None)
        with self.assertRaises(nh.policy.Dead):
            p.send(" ")

    def test_failed_farlook_judges_by_glyph_and_never_throws_at_it(self):
        p = self.pilot([" -------- ", " |......| ", " |..@d..| ", " |......| ", " -------- "], (3, 4), {})
        c = p.context(p.term.view())
        self.assertEqual(c["obst"], [])
        self.assertTrue(c["hostiles"][0]["name"].startswith("likely "))

    def test_deadly_incoming_damage_reorders_the_ladder(self):
        p = self.pilot([" -------- ", " |......| ", " |..@a..| ", " |......| ", " -------- "], (3, 4),
                       {(3, 5): "soldier ant"})
        v = p.term.view()
        c = p.context(v)
        self.assertGreaterEqual(c["incoming"], 30)
        c["hp"], c["can_pray"] = 6, False
        p.crisis = {"until": 999, "dl": 3, "hp": 12, "tried": [], "why": "test"}
        acts = p.actions(v, c)
        self.assertTrue(acts[0].key.startswith("attack"), [a.key for a in acts[:3]])

    def test_stair_keys_are_not_bundled_with_travel(self):
        p = self.pilot([" --------- ", " |......>| ", " |..@....| ", " --------- "], (3, 4), {})
        v = p.term.view()
        p.view()
        c = p.context(v)
        go = [a for a in p.actions(v, c) if a.key == "goto_stairs"]
        self.assertTrue(go and not go[0].keys.endswith(">"))

    def test_stair_ping_pong_is_a_camped_report(self):
        p = nh.Pilot(FakeTerm(), None)
        p.fled_from[4] = [("dwarf", (11, 39))]
        p.level_trail.extend([3, 4, 3, 4])
        reason = p.oscillation(3)
        self.assertTrue(reason.startswith("camped: the Dlvl 4 arrival is camped by dwarf at 12,40"), reason)
        self.assertEqual(nh.escalation.classify(reason), "camped")
        self.assertEqual(p.level(4).stair_ban_until, 0)       # the deeper level's way up stays open

    def test_settings_keep_an_hp_floor(self):
        with self.assertRaises(ValueError):
            nh.apply_settings(None, {"rest_hp": "0.3", "hp_escalate": "0", "crisis_turns": "1000"})


class ClosedShopTest(Case):
    ROWS = [" ------- ", " |.....| ", " |.....+ ", " ------- "]

    def test_the_engraving_marks_the_doors_and_reports_say_do_not_kick(self):
        p = nh.Pilot(FakeTerm(screen("", self.ROWS + ["         @ "]), (5, 8)), None)
        v = p.term.view()
        p.message('Something is written here in the dust.  You read: "Closed for inventory".', v)
        lv = p.lv[3]
        self.assertIn((4, 8), lv.shop_doors)               # the door north of the engraved square
        lv.locked.add((4, 8))
        c = {"lv": lv, "watch": False, "peace": [], "obst": []}
        self.assertFalse(p.kickable((4, 8), c))
        self.assertIn("closed shop, do not kick", p.door_notes(dict(c, hero=(5, 8))))

    def test_kick_reads_the_engraving_first(self):
        term = FakeTerm(screen('You read: "Closed for inventory".', self.ROWS + ["         @ "]), (5, 8))
        p = nh.Pilot(term, None)
        lv = p.level(3)
        lv.locked.add((4, 8))
        c = {"lv": lv, "hero": (5, 8), "dl": 3, "turn": 400}
        p.do(term.view(), c, nh.Act("kick_k", "Kick open the locked door to the north", "\x04k", "kick", 4.0,
                                    (4, 8)))
        self.assertNotIn("\x04k", term.sent)
        self.assertIn((4, 8), lv.shop_doors)


class BrokenDoorTest(Case):
    def test_a_broken_door_is_no_longer_a_closed_door(self):
        p = nh.Pilot(FakeTerm(), None)
        lv = p.level(3)
        lv.terr[(2, 4)], lv.tfg[(2, 4)] = "+", "brown"
        lv.locked.add((2, 4))
        p.last_act = nh.Act("open_l", "open", "ol", "door", 3, (2, 4))
        p.message("This door is broken.", p.term.view())
        self.assertEqual(lv.terr[(2, 4)], ".")
        self.assertNotIn((2, 4), lv.locked)


class SwarmTest(Case):
    def test_a_swarm_sends_the_hero_up(self):
        rows = [" ---------- ", " |<.......| ", " |..@.aaa.| ", " |........| ", " ---------- "]
        p = nh.Pilot(FakeTerm(screen("", rows), (3, 4)), None)
        p.lookup = lambda pos: "killer bee"
        p.options = p.briefed = True
        p.inv_turn = 10 ** 9
        v = p.term.view()
        p.view()
        c = p.context(v)
        self.assertEqual(len(c["swarm"]), 3)
        acts = p.actions(v, c)
        self.assertEqual(acts[0].key, "flee_swarm", [a.key for a in acts[:3]])
        p.swarm_fled = (3, "3 killer bee")
        c2 = dict(c, dl=2)
        with self.assertRaises(nh.policy.Hard) as e:
            p.bookkeep(v, c2)
        self.assertTrue(str(e.exception).startswith("swarm: 3 killer bee"))


class StepAroundTest(Case):
    def test_travel_steps_around_a_monster_it_must_not_melee(self):
        rows = [" ---------- ", " |........| ", " |..F@....| ", " |........| ", " |.>......| ", " ---------- "]
        p = nh.Pilot(FakeTerm(screen("", rows), (3, 5)), None)
        p.lookup = lambda pos: "acid blob" if pos == (3, 4) else None
        p.options = p.briefed = True
        p.inv_turn = 10 ** 9
        v = p.term.view()
        p.view()
        c = p.context(v)
        lv = c["lv"]
        lv.downs = {(5, 3): "main"} if isinstance(lv.downs, dict) else [(5, 3)]
        a = nh.Act("goto_stairs", "Travel to the down stairs", nh.level.travel((3, 5), (5, 3)), "travel", 5, (5, 3))
        acts = [a]
        p.through_doors(v, c, acts)
        self.assertEqual(len(acts[0].keys), 1, acts[0].keys)
        self.assertIn("around the acid blob", acts[0].desc)


class ChivalryTest(Case):
    def knight(self, look):
        rows = [" ---------- ", " |........| ", " |..d@....| ", " |........| ", " ---------- "]
        p = nh.Pilot(FakeTerm(screen("", rows), (3, 5)), None)
        p.lookup = look
        p.role = "Knight"
        p.options = p.briefed = True
        p.inv_turn = 10 ** 9
        p.view()
        return p, p.term.view()

    def test_a_knight_leaves_a_sleeping_monster_alone(self):
        p, v = self.knight(lambda pos: "jackal, asleep")
        c = p.context(v)
        self.assertTrue(c["hostiles"][0]["caitiff"])
        self.assertFalse([a for a in p.actions(v, c) if a.kind == "attack"])
        p.crisis = {"until": 10 ** 6, "dl": 1, "hp": 1, "tried": [], "why": "test"}
        self.assertTrue([a for a in p.actions(v, c) if a.kind == "attack"])     # the ladder may still fight

    def test_a_knight_leaves_a_fleeing_monster_alone(self):
        p, v = self.knight(lambda pos: "jackal")
        c = p.context(v)
        self.assertTrue([a for a in p.actions(v, c) if a.kind == "attack"])
        p.message("You hit the jackal.  The jackal turns to flee.", v)
        c = p.context(v)
        self.assertTrue(c["hostiles"][0].get("fleeing"))
        self.assertFalse([a for a in p.actions(v, c) if a.kind == "attack"])

    def test_other_roles_still_fight(self):
        p, v = self.knight(lambda pos: "jackal, asleep")
        p.role = "Valkyrie"
        c = p.context(v)
        self.assertTrue([a for a in p.actions(v, c) if a.kind == "attack"])


class MimicTest(Case):
    def test_after_a_boulder_mimic_the_other_boulders_are_suspect(self):
        rows = [" ---------- ", " |........| ", " |...@0...| ", " |........| ", " ---------- "]
        p = nh.Pilot(FakeTerm(screen("", rows), (3, 5)), None)
        p.lookup = lambda pos: None
        p.options = p.briefed = True
        p.inv_turn = 10 ** 9
        v = p.term.view()
        p.view()
        c = p.context(v)
        push = [a for a in p.actions(v, c) if a.key.startswith("push_")]
        p.message("That boulder is a small mimic!", v)
        self.assertIn("boulder", c["lv"].disguises)
        again = [a for a in p.actions(v, c) if a.key.startswith("push_")]
        self.assertTrue(all(a.prior <= -4 for a in again))
        self.assertTrue(not push or push[0].prior > -4)


class BlindTest(Case):
    def blind(self, inv, hp="HP:5(16)"):
        rows = [" ---------- ", " |........| ", " |..I@....| ", " |........| ", " ---------- "]
        p = nh.Pilot(FakeTerm(screen("", rows, status2="Dlvl:2 $:0 %s Pw:5(5) AC:6 Xp:1 T:60 Blind" % hp),
                              (3, 5)), None)
        p.lookup = lambda pos: None
        p.options = p.briefed = True
        p.inv_turn = 10 ** 9
        p.inv = inv
        v = p.term.view()
        p.view()
        return p, v, p.context(v)

    def test_a_unicorn_horn_cures_blindness_first(self):
        p, v, c = self.blind({"h": ("an uncursed unicorn horn", "Tools")})
        self.assertTrue(c["blind"])
        acts = p.actions(v, c)
        self.assertEqual(acts[0].key, "cure", [(a.key, a.prior) for a in acts[:4]])
        self.assertEqual(acts[0].keys, "ah")

    def test_blind_and_hurt_the_unseen_marker_is_not_the_first_choice(self):
        p, v, c = self.blind({})
        acts = p.actions(v, c)
        attack = [a for a in acts if a.key.startswith("attack_")]
        self.assertTrue(attack and attack[0].prior <= 0.5)
        ladder = p.crisis_ladder(v, c, acts)
        self.assertFalse([a for a in ladder if a.key.startswith("attack_")])


class StalledTravelTest(Case):
    def test_after_travel_made_no_progress_the_route_is_walked_by_hand(self):
        rows = [" ---------- ", " |........| ", " |....@...| ", " |........| ", " |.>......| ", " ---------- "]
        p = nh.Pilot(FakeTerm(screen("", rows), (3, 6)), None)
        p.lookup = lambda pos: None
        p.options = p.briefed = True
        p.inv_turn = 10 ** 9
        v = p.term.view()
        p.view()
        c = p.context(v)
        a = nh.Act("goto_stairs", "Travel to the down stairs", nh.level.travel((3, 6), (5, 3)), "travel", 5, (5, 3))
        acts = [a]
        p.through_doors(v, c, acts)
        self.assertTrue(acts[0].keys.startswith("_"))
        c["lv"].failed[(5, 3)] = 1
        acts = [a]
        p.through_doors(v, c, acts)
        self.assertEqual(len(acts[0].keys), 1, acts[0].keys)
        self.assertIn("by hand", acts[0].desc)


class SoftBlockerTest(Case):
    def test_walled_in_by_an_acid_blob_with_nothing_to_throw_the_hero_hits_it(self):
        p = nh.Pilot(FakeTerm(), None)
        c = {"hero": (5, 5), "lv": p.level(2), "dist": {(5, 5): 0}, "hostiles": [], "hp": 16,
             "obst": [{"name": "acid blob", "pos": (5, 6), "dist": 1}]}
        p.inv = {}
        acts = p.ladder_actions(p.term.view(), c)
        self.assertIn("attack_l", [a.key for a in acts])
        c["hp"] = 12
        self.assertNotIn("attack_l", [a.key for a in p.ladder_actions(p.term.view(), c)])
        c["hp"], c["obst"][0]["name"] = 30, "gas spore"
        self.assertNotIn("attack_l", [a.key for a in p.ladder_actions(p.term.view(), c)])


class LineUpTest(Case):
    def test_the_hero_walks_into_line_to_throw_at_a_gas_spore(self):
        rows = [" ------------ ", " |..........| ", " |.@........| ", " |..........| ", " |.....e....| ",
                " |..........| ", " ------------ "]
        p = nh.Pilot(FakeTerm(screen("", rows), (3, 3)), None)
        p.lookup = lambda pos: "gas spore" if pos == (5, 7) else None
        p.options = p.briefed = True
        p.inv_turn = 10 ** 9
        p.inv = {"b": ("2 +0 daggers", "Weapons")}
        v = p.term.view()
        p.view()
        c = p.context(v)
        c["frontier"] = []
        line = p.line_up(v, c)
        self.assertIsNotNone(line)
        q = line.target
        dr, dc = 5 - q[0], 7 - q[1]
        self.assertTrue(dr == 0 or dc == 0 or abs(dr) == abs(dc))
        self.assertGreaterEqual(max(abs(dr), abs(dc)), 2)


class StuckBoulderTest(Case):
    def test_a_stuck_boulder_is_broken_with_a_wand_of_striking(self):
        rows = [" ---------- ", " |........| ", " |...@0...| ", " |........| ", " ---------- "]
        p = nh.Pilot(FakeTerm(screen("", rows), (3, 5)), None)
        p.lookup = lambda pos: None
        p.options = p.briefed = True
        p.inv_turn = 10 ** 9
        p.inv = {"f": ("a wand of striking (0:5)", "Wands")}
        v = p.term.view()
        p.view()
        c = p.context(v)
        c["lv"].stuck_boulders.add((3, 6))
        acts = p.ladder_actions(v, c)
        brk = [a for a in acts if a.key == "break_l"]
        self.assertTrue(brk and brk[0].keys == "zfl", [a.key for a in acts])
