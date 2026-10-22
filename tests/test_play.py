import json
import re
import tempfile
from pathlib import Path
from unittest import TestCase

from helpers import FakeTerm, screen, view
from nethack_harness.actions import Action
from nethack_harness.perceive import parse_menu_entries
from nethack_harness.play import Play, hint, messages
from nethack_harness.screen import View
from nethack_harness.session import Session
from nethack_harness.settings import Settings
from nethack_harness.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "screens"


def recorded(name, message=None, cursor=None, turn=None):
    data = json.loads((FIXTURES / name).read_text())
    if message is not None:
        data["rows"][0] = message.ljust(80)
    if cursor is not None:
        data["cursor"] = list(cursor)
    if turn is not None:
        data["rows"][23] = data["rows"][23].replace("T:1 ", "T:%d " % turn)
    return View(**data)


def start(message=None, cursor=None, turn=None):
    return recorded("seed1001-start.json", message, cursor, turn)


def farlook(name, turn=None):
    return [start("Pick a monster, object or location.", turn=turn),
            start("d        a dog or other canine (%s)" % name, turn=turn)]


STAIRS = start("There is a staircase up out of the dungeon here.")
# A recorded game at T:43: the hero walked the corridor from the stairs room to the lower room, leaving a two-wide
# stretch at 13,5 and 13,6 unwalked, with the squares east of it never seen. WALK is every square stood on, as x,y.
CORRIDOR = json.loads((FIXTURES / "corridor-blob.json").read_text())
WALK = [(4, 4), (5, 4), (6, 3), (6, 5), (6, 6), (6, 7), (6, 8), (6, 9), (6, 10), (6, 11), (6, 12), (6, 13), (6, 14),
        (6, 15), (6, 16), (6, 17), (6, 18), (7, 3), (7, 6), (8, 4), (8, 6), (9, 4), (9, 6), (10, 4), (10, 6), (11, 4),
        (11, 6), (12, 4), (12, 5), (7, 19)]


def walked(data, x, y, turn=None, under="."):
    """A copy of a screen with the hero moved to x,y, leaving `under` where it stood."""
    data = json.loads(json.dumps(data))
    rows = [list(row) for row in data["rows"]]
    r, c = data["cursor"]
    rows[r][c] = under
    rows[y][x] = "@"
    if turn is not None:
        rows[23] = list(re.sub(r"T:\d+", "T:%d" % turn, "".join(rows[23])))
    data["rows"], data["cursor"] = ["".join(row) for row in rows], [y, x]
    return View(**data)


def column(lines, x, y):
    """The character printed at x,y, read through the ruler and the row numbers."""
    units = next(index for index, line in enumerate(lines) if line.startswith("   ") and line.strip().isdigit())
    left = int(lines[units - 1].split()[0])
    row = next(line for line in lines[units + 1:] if line[:3] == "%2d " % y)
    return row[3 + x - left]


def moved(data, frm, to, turn):
    """The start screen with the monster at frm moved to to, at a later turn."""
    for grid in ("rows", "fg", "bold", "rev"):
        rows = [list(row) for row in data[grid]]
        rows[to[0]][to[1]], rows[frm[0]][frm[1]] = rows[frm[0]][frm[1]], rows[to[0]][to[1]]
        data[grid] = ["".join(row) for row in rows] if grid == "rows" else rows
    data["rows"][23] = data["rows"][23].replace("T:1 ", "T:%d " % turn)
    return data


class Engine:
    model = None


class PlayTest(TestCase):
    def play(self, views, max_action_steps=8, engine=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = Store(directory.name)
        self.addCleanup(store.close)
        return Play(Session(FakeTerm(views), engine, store, Settings(max_action_steps=max_action_steps)))

    def test_look_names_monsters_and_underfoot_once_and_coordinates_read_off_the_map(self):
        play = self.play([start()] + farlook("tame little dog") + farlook("jackal") + [STAIRS])
        text = play.view(inquire=True)
        lines = text.splitlines()
        self.assertEqual(play.session.term.sent, [";", "@k.", ";", "@llll.", ":"])
        self.assertIn("monsters: d tame little dog 32,5 adjacent k; d jackal 36,6 4 away", lines)
        self.assertEqual(lines[lines.index("monsters: d tame little dog 32,5 adjacent k; d jackal 36,6 4 away") - 1],
                         "you: 32,6  here: < up stairs; There is a staircase up out of the dungeon here.")
        self.assertIn("seen: + door 32,7 (adjacent j); + door 30,5 (1)", lines)
        self.assertEqual(lines[0], "Dlvl:1 $:0 HP:14(14) Pw:5(5) AC:4 Xp:1 T:1")
        self.assertEqual([column(lines, x, y) for x, y in ((32, 6), (32, 5), (36, 6), (30, 5), (33, 4))],
                         ["@", "d", "d", "+", "/"])
        observation = play.session.observe()
        self.assertEqual(observation["hero"]["position"], [32, 6])
        self.assertEqual([e["position"] for e in observation["entities"]], [[33, 4], [32, 5], [36, 6]])
        self.assertFalse(any("a dog or other canine" in line for line in lines))
        play.view(inquire=True)
        self.assertEqual(len(play.session.term.sent), 5)

    def test_a_farlook_answer_that_runs_to_more_is_recorded_in_full(self):
        before = recorded("seed4242-start.json")
        result = recorded("seed4242-farlook-more.json")
        prompt = recorded("seed4242-start.json", "Pick a monster, object or location.")
        zombie = recorded("seed4242-start.json", "Z        a zombie (kobold zombie)")
        play = self.play([before, prompt, result, before, prompt, zombie, before])
        lines = play.view(inquire=True).splitlines()
        self.assertEqual(play.session.term.sent[:5], [";", "@l.", " ", ";", "@j."])
        self.assertIn("monsters: d tame little dog 19,6 adjacent l; Z kobold zombie 18,7 adjacent j", lines)

    def test_names_never_come_from_another_monster_square_or_turn(self):
        later = moved(json.loads((FIXTURES / "seed1001-start.json").read_text()), (6, 36), (6, 37), 2)
        play = self.play([start()] + farlook("tame little dog") + farlook("jackal") + [STAIRS])
        play.view(inquire=True)
        unlooked = View(**later)
        play.session.term.views = [unlooked]
        lines = play.view(inquire=True).splitlines()
        self.assertIn("monsters: d tame little dog (looked T:1) 32,5 adjacent k; d 37,6 5 away", lines)
        self.assertEqual(play.session.term.sent[5:], [";"])
        play.observer.current.inspections[(6, 37)] = {"description": "jackal", "observed_turn": 1, "glyph": "d",
                                                       "colour": unlooked.col(6, 37), "source": "inspect"}
        play.session.term.views = [unlooked]
        self.assertNotIn("jackal", play.view(inquire=True))

    def test_naming_waits_until_the_game_is_verifiably_waiting_for_a_command(self):
        cursor = start("floor of a room", cursor=(6, 34))
        self.assertEqual(cursor.hero, (6, 34))
        for unsure in (cursor, start(cursor=(5, 32))):
            with self.subTest(cursor=unsure.cursor):
                play = self.play([unsure])
                play.view(inquire=True)
                self.assertEqual(play.session.term.sent, [])

    def test_look_while_the_engine_plays_sends_nothing(self):
        play = self.play([start()], engine=Engine())
        text = play.view()
        self.assertEqual(play.session.term.sent, [])
        self.assertIn("monsters: d pet 32,5 adjacent k; d 36,6 4 away", text.splitlines())
        self.assertEqual(list(play.session.store.records()), [])

    def test_looks_in_a_paused_engine_session_keep_the_engines_evidence(self):
        play = self.play([start()] + farlook("tame little dog") + farlook("jackal") + [STAIRS], engine=Engine())
        session = play.session
        session.step(manual=Action("manual", "Caller-supplied keys", "manual", "s"))
        session.term.views = [start()] + farlook("tame little dog") + farlook("jackal") + [STAIRS]
        session.failed_action = marker = (("fingerprint", "objective", None), 7)
        before = session.observe()
        positions = list(session.navigation.positions)
        messages = list(play.observer.messages)
        lines = play.view(inquire=True).splitlines()
        self.assertEqual(session.term.sent[1:], [";", "@k.", ";", "@llll.", ":"])
        self.assertIn("monsters: d tame little dog 32,5 adjacent k; d jackal 36,6 4 away", lines)
        self.assertIn("you: 32,6  here: < up stairs; There is a staircase up out of the dungeon here.", lines)
        after = session.observe()
        self.assertIs(session.failed_action, marker)
        self.assertEqual(list(play.observer.messages), messages)
        self.assertEqual(after["objective_progress"]["recent_attempts"], before["objective_progress"]["recent_attempts"])
        self.assertEqual(list(session.navigation.positions), positions)
        self.assertEqual([r["source"] for r in session.store.records()], ["manual"] + ["protocol"] * 3)

    def test_command_messages_include_paged_windows_and_repeats_but_not_unchanged_text(self):
        frames = [{"phase": "more", "message": "You miss the jackal.--More--"},
                  {"phase": "more", "message": "You miss the jackal.--More--"},
                  {"phase": "more", "message": "There is a staircase up here.",
                   "window": ["There is a staircase up here.", "", "Things that are here:", "7 apples"]},
                  {"phase": "menu", "message": "Pick up what?"},
                  {"phase": "play", "message": "Beware, there will be no return!", "message_unchanged": True}]
        self.assertEqual(messages(frames), ["You miss the jackal.", "You miss the jackal.",
                                            "There is a staircase up here.", "  Things that are here:", "  7 apples"])

    def test_a_look_after_a_command_does_not_repeat_its_messages(self):
        quiet = view(screen("", [" ------ ", " |.@..| ", " ------ "]), (2, 3))
        heard = view(screen("You hear a door open.", [" ------ ", " |.@..| ", " ------ "]), (2, 3))
        play = self.play([quiet, heard], engine=Engine())
        play.view()
        report, _, _ = play.send("s")
        self.assertIn("msg: You hear a door open.", play.view(report).splitlines())
        self.assertNotIn("msg:", play.view())

    def test_go_walks_the_nearest_named_target_and_stops_on_a_message(self):
        def at(col, turn, message=""):
            row = list(" |....>| ")
            row[col] = "@"
            return view(screen(message, [" ------- ", "".join(row), " ------- "],
                               status2="Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:%d" % turn), (2, col))

        play = self.play([at(2, 400), at(3, 401), at(4, 402, "You hear a door open.")], engine=Engine())
        play.view()
        report, reason, refused = play.go(">")
        outcome = play.session.last["outcome"]
        self.assertEqual((reason, refused), (None, False))
        self.assertEqual(play.session.term.sent, ["ml", "ml"])
        self.assertEqual((outcome["reason"], outcome["changed_fields"], outcome["steps"]),
                         ("observation_changed", ["message"], 2))
        self.assertEqual(outcome["action"]["target"], [6, 2])
        self.assertIn("msg: You hear a door open.", play.view(report).splitlines())
        for target, error in (("altar", "no remembered"), ("99,1", "off the map"), ("4,2", "already at"),
                              ("9,9", "has not been seen")):
            with self.subTest(target=target):
                with self.assertRaisesRegex(ValueError, error):
                    play.go(target)
        self.assertEqual(len(play.session.term.sent), 2)

    def test_go_door_walks_beside_another_door_when_already_beside_one(self):
        play = self.play([start()], engine=Engine())
        play.observer.observation(start())
        play.go("door")
        self.assertEqual(play.session.last["outcome"]["action"]["target"], [31, 5])

    def test_go_to_a_closed_door_square_walks_beside_it(self):
        play = self.play([start()], engine=Engine())
        play.observer.observation(start())
        play.go("30,5")
        self.assertEqual(play.session.last["outcome"]["action"]["target"], [31, 5])
        with self.assertRaisesRegex(ValueError, "already at 32,6"):
            play.go("32,7")

    def test_go_names_the_monster_blocking_the_only_route(self):
        play = self.play([view(screen("", ["", " >##F#@"]), (2, 6))])
        play.observer.observation(view(screen("", ["", " >####@"]), (2, 6)))
        with self.assertRaisesRegex(ValueError, "blocked by a monster at 4,2"):
            play.go(">")
        self.assertEqual(play.session.term.sent, [])

    def test_a_named_corridor_end_is_the_destination_and_frontier_explores_on(self):
        for target, kind in (("5,2", "travel"), ("frontier", "explore")):
            with self.subTest(target=target):
                play = self.play([view(screen("", ["", "   @##"]), (2, 3))], max_action_steps=4, engine=Engine())
                play.view()
                play.go(target)
                self.assertEqual(play.session.last["outcome"]["action"]["kind"], kind)

    def test_frontier_skips_the_rock_beside_a_known_corridor(self):
        play = self.play([view(screen("", ["", "   @#####"]), (2, 3))], engine=Engine())
        lines = play.view().splitlines()
        self.assertIn("frontier: 8,2 (5)", lines)

    def test_frontier_reaches_a_corridor_stretch_the_hero_never_walked(self):
        play = self.play([View(**CORRIDOR)], engine=Engine())
        for x, y in WALK:
            play.observer.observation(walked(CORRIDOR, x, y))
        lines = play.view().splitlines()
        frontier = next(line for line in lines if line.startswith("frontier: "))
        self.assertRegex(frontier, r"^frontier: 13,[56] \(\d+\)$")
        play.go("frontier")
        self.assertEqual(play.session.last["outcome"]["action"]["target"], [int(n) for n in frontier[10:15].split(",")])
        self.assertEqual(play.session.term.sent[:1], ["mk"])

    def test_a_corridor_bend_the_walk_cut_across_is_no_frontier(self):
        # The hero stepped diagonally from 4,2 to 5,3; the corner at 5,2 was never stood on.
        corridor = {"rows": screen("", ["", "   ###", "     #", "     #"]), "cursor": [2, 3],
                    "fg": [["default"] * 80 for _ in range(24)], "bold": [[False] * 80 for _ in range(24)],
                    "rev": [[False] * 80 for _ in range(24)]}
        end = [walked(corridor, 5, 4, turn, "#") for turn in (403, 404, 405)]
        play = self.play(end, engine=Engine())
        for turn, (x, y) in enumerate(((3, 2), (4, 2), (5, 3), (5, 4)), start=400):
            play.observer.observation(walked(corridor, x, y, turn, "#"))
        self.assertNotIn("frontier:", play.view())
        play.rest(2)
        with self.assertRaisesRegex(ValueError, r"corridor ends: 3,2 \(searched 0\), 5,4 \(searched 2\)$"):
            play.go("frontier")

    def test_no_frontier_lists_what_remains(self):
        # The + at 8,4 is in no wall line the screen shows and on no known floor: a door or an object.
        rows = [" -----", " |...|", " |.@.+", " |...|  +", " -----"]
        play = self.play([view(screen("", rows), (3, 3))], engine=Engine())
        play.view()
        with self.assertRaisesRegex(ValueError, r"doors: 5,3 \(1\), 8,4 door or object \(no route\)"):
            play.go("frontier")

    def test_go_and_rest_stop_when_the_caller_interrupts(self):
        def at(col, turn):
            row = list(" |.....>| ")
            row[col] = "@"
            return view(screen("", [" -------- ", "".join(row), " -------- "],
                               status2="Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:%d" % turn), (2, col))

        for command, argument in (("go", ">"), ("rest", 5)):
            with self.subTest(command=command):
                play = self.play([at(2, 400), at(3, 401), at(4, 402), at(5, 403)], engine=Engine())
                play.view()
                getattr(play, command)(argument, lambda: bool(play.session.term.sent))
                self.assertEqual(play.session.last["outcome"]["reason"], "caller_interrupt")
                self.assertEqual(len(play.session.term.sent), 1)

    def test_rest_refuses_at_a_prompt_and_reports_a_search_the_game_refused(self):
        prompt = view(screen("What do you want to eat? [k or ?*]"), (0, 35))
        play = self.play([prompt])
        with self.assertRaisesRegex(ValueError, "prompt is open"):
            play.rest(3)
        self.assertEqual(play.session.term.sent, [])
        refused = view(screen("You already found a monster.  Use 'm' prefix to force another search.",
                              [" ---- ", " |.@d| ", " ---- "]), (2, 3))
        play = self.play([view(screen("", [" ---- ", " |.@d| ", " ---- "]), (2, 3)), refused], engine=Engine())
        play.view()
        report, _, failed = play.rest(3)
        self.assertTrue(failed)
        self.assertEqual(play.session.last["outcome"]["elapsed_turns"], 0)

    def test_hints_offer_the_choices_on_screen(self):
        inventory = recorded("seed4242-inventory.json")
        keys = [entry["key"] for entry in parse_menu_entries(inventory)]
        self.assertTrue(hint(inventory, "menu").startswith("keys: %s select" % " ".join(keys)))
        self.assertEqual(hint(recorded("seed4242-quit.json"), "ended"), "keys: y n q")
        self.assertIn("one of [k or ?*]", hint(view(screen("What do you want to eat? [k or ?*]"), (0, 35)), "item"))

    def test_game_over_prompt_is_answerable_and_shown_with_its_choices(self):
        text = self.play([recorded("seed4242-quit.json")]).view()
        self.assertIn("screen (game over):", text)
        self.assertTrue(text.endswith("keys: y n q"))

    def test_non_ascii_map_symbols_are_refused(self):
        dec = view(screen("", [" ----", " |·@·|"]), (2, 3))
        with self.assertRaisesRegex(ValueError, "ASCII"):
            self.play([dec]).view()

    def test_view_sizes_stay_within_budget(self):
        budgets = (("seed1001-start.json", 600), ("seed11-0240.json", 1300), ("seed7-0240.json", 1900),
                   ("seed4242-inventory.json", 1300), ("seed4242-quit.json", 500))
        for name, budget in budgets:
            with self.subTest(fixture=name):
                self.assertLessEqual(len(self.play([recorded(name)], engine=Engine()).view()), budget)
