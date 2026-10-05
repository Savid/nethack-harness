from unittest import TestCase
from unittest import mock

from helpers import FakeTerm, screen, view
from nethack_harness.actions import Action, catalogue
from nethack_harness.execute import Executor
from nethack_harness.perceive import Observer, fingerprint


def room(col=3, hp=12, turn=400, message="", monster=None):
    row = list(" |.........| ")
    row[col] = "@"
    if monster:
        row[monster] = "d"
    return view(screen(message, [" ----------- ", "".join(row), " ----------- "],
                       status2="Dlvl:3 $:0 HP:%d(16) Pw:2(2) AC:6 Xp:2 T:%d" % (hp, turn)), (2, col))


def corridor(hero, tiles, turn=400, hp=12):
    rows = [[" "] * 80 for _ in range(21)]
    for (r, c), glyph in tiles.items():
        rows[r - 1][c] = glyph
    rows[hero[0] - 1][hero[1]] = "@"
    return view(screen("", ["".join(row) for row in rows],
                       status2="Dlvl:3 $:0 HP:%d(16) Pw:2(2) AC:6 Xp:2 T:%d" % (hp, turn)), hero)


class ExecutionTest(TestCase):
    def executor(self, frames):
        term, observer = FakeTerm(frames), Observer()
        observer.observation(term.view())
        records = []

        def record(keys, source):
            records.append({"keys": keys, "source": source, "status": "pending"})
            return len(records) - 1

        def result(index, status):
            records[index]["status"] = status

        return Executor(term, observer, record, result), records

    def test_life_saving_continues_past_initial_death_message(self):
        executor, _ = self.executor([room(), view(screen("You die...--More--"), (0, 20)),
                                    view(screen("But wait... Your medallion begins to glow!--More--"), (0, 62)),
                                    room(hp=16, turn=401, message="You feel much better!")])
        result = executor.run(Action("attack:l", "Attack east", "attack", "Fl"), fingerprint(executor.term.view()))
        self.assertEqual(executor.term.sent, ["Fl", " ", " "])
        self.assertEqual(result["reason"], "completed")
        self.assertIsNone(executor.observer.observation(executor.term.view())["game_result"])


    def test_bounded_travel_executes_exact_route(self):
        executor, records = self.executor([room(3), room(4, turn=401), room(5, turn=402)])
        result = executor.run(Action("travel:3,6", "Go east", "travel", steps=2, target=(2, 5),
                                     route=((2, 4), (2, 5))), fingerprint(executor.term.view()))
        self.assertEqual(executor.term.sent, ["ml", "ml"])
        self.assertEqual((result["reason"], result["steps"], result["elapsed_turns"]), ("completed", 2, 2))
        self.assertTrue(all(r["status"] == "completed" for r in records))

    def test_failed_navigation_distinguishes_unchanged_and_changed_observations(self):
        for kind in ("travel", "explore"):
            for after, reason in ((room(3), "no_observed_effect"),
                                  (room(3, turn=401, message="You cannot pass."), "movement_interrupted")):
                with self.subTest(kind=kind, reason=reason):
                    executor, _ = self.executor([room(3), after, room(4, turn=402)])
                    result = executor.run(Action(kind + ":3,5", "Go east", kind, steps=8,
                                                 target=(2, 4), route=((2, 4),)),
                                          fingerprint(executor.term.view()))
                    self.assertEqual(executor.term.sent, ["ml"])
                    self.assertEqual((result["reason"], result["steps"]), (reason, 1))

    def test_direction_followup_waits_for_the_expected_prompt(self):
        direction = view(screen("In what direction?"), (0, 18))
        executor, records = self.executor([room(), direction, room(turn=401)])
        result = executor.run(Action("open:l", "Open east", "open", "o", target=(2, 4),
                                     followups=(("direction", "l"),)), fingerprint(executor.term.view()))
        self.assertEqual(executor.term.sent, ["o", "l"])
        self.assertEqual((result["steps"], result["elapsed_turns"]), (1, 1))
        self.assertEqual([item["source"] for item in records], ["action", "action"])

    def test_inspection_records_only_after_its_checked_target_selection(self):
        before = room(monster=4)
        targeting = room(message="Pick a monster, object or location.", monster=4)
        identified = room(message="d  a jackal (jackal)", monster=4)
        executor, _ = self.executor([before, targeting, identified])
        action = next(a for a in catalogue(before, executor.observer, 8) if a.id == "inspect:3,5")
        with mock.patch.object(executor.observer, "remember_look", wraps=executor.observer.remember_look) as remember:
            result = executor.run(action, fingerprint(before))
        self.assertEqual(executor.term.sent, [";", "@l."])
        self.assertEqual(result["reason"], "completed")
        self.assertEqual(result["steps"], 1)
        remember.assert_called_once()
        entity, = executor.observer.observation(executor.term.view())["entities"]
        self.assertEqual(entity["last_inspection"]["description"], "jackal")

    def test_unexpected_prompt_does_not_receive_a_queued_direction(self):
        for rejected in (room(message="You cannot open anything."),
                         view(screen("Really kick your pony? [yn] (n)"), (0, 32))):
            with self.subTest(message=rejected.msg):
                executor, _ = self.executor([room(), rejected, room(turn=401)])
                result = executor.run(Action("kick:l", "Kick east", "kick", "\x04", target=(2, 4),
                                             followups=(("direction", "l"),)), fingerprint(executor.term.view()))
                self.assertEqual(executor.term.sent, ["\x04"])
                self.assertEqual(result["reason"], "unexpected_prompt")
                self.assertEqual(result["steps"], 1)

    def test_caller_can_interrupt_a_checked_followup(self):
        direction = view(screen("In what direction?"), (0, 18))
        executor, _ = self.executor([room(), direction, room(turn=401)])
        result = executor.run(Action("open:l", "Open east", "open", "o", followups=(("direction", "l"),)),
                              fingerprint(executor.term.view()), cancelled=lambda: bool(executor.term.sent))
        self.assertEqual(executor.term.sent, ["o"])
        self.assertEqual(result["reason"], "caller_interrupt")

    def test_changed_observation_returns_to_engine_between_steps(self):
        for changed, field in ((room(4, hp=10, turn=401), "status"), (room(4, turn=401, monster=7), "entities"),
                               (room(4, turn=401, message="You hear a sound."), "message")):
            with self.subTest(change=changed.msg or changed.st):
                executor, _ = self.executor([room(3), changed, room(5, turn=402)])
                result = executor.run(Action("travel:3,6", "Go east", "travel", steps=2,
                                             route=((2, 4), (2, 5))), fingerprint(executor.term.view()))
                self.assertEqual(executor.term.sent, ["ml"])
                self.assertEqual(result["reason"], "observation_changed")
                self.assertIn(field, result["changed_fields"])

    def test_stale_choice_sends_nothing(self):
        executor, records = self.executor([room()])
        expected = fingerprint(executor.term.view())
        executor.term.on_poll = lambda: setattr(executor.term, "views", [room(hp=8)])
        result = executor.run(Action("wait:1", "Wait", "wait", "."), expected)
        self.assertEqual(result["reason"], "observation_changed")
        self.assertEqual(records, [])

    def test_observation_changed_by_final_transport_poll_sends_nothing(self):
        executor, records = self.executor([room()])
        polls = []

        def change_before_send():
            polls.append(None)
            if len(polls) == 2:
                executor.term.views = [view(screen("In what direction?"), (0, 18))]

        executor.term.on_poll = change_before_send
        result = executor.run(Action("move:l", "Move east", "move", "l"), fingerprint(executor.term.view()))
        self.assertEqual(result["reason"], "observation_changed")
        self.assertEqual(executor.term.sent, [])
        self.assertEqual(records, [])

    def test_repeated_failed_moves_are_summarized_and_reset_by_another_action(self):
        executor, _ = self.executor([room()])
        action = Action("move:h", "Move west", "move", "h", target=(2, 2))
        for _ in range(4):
            executor.run(action, fingerprint(executor.term.view()))
        observation = executor.observer.observation(executor.term.view())
        self.assertEqual(observation["repetition"], {"action": "move:h", "consecutive_in_recent_history": 4,
                                                   "elapsed_turns": 0, "position_unchanged": True})
        self.assertEqual(observation["recent_actions"][-1]["target"], [3, 3])
        self.assertEqual(len(observation["recent_actions"]), 1)
        self.assertEqual(observation["recent_actions"][0]["repetitions"], 4)
        self.assertEqual(len(executor.observer.history), 4)
        executor.run(Action("look_here", "Look here", "look_here", ":"), fingerprint(executor.term.view()))
        self.assertEqual(executor.observer.repetition()["consecutive_in_recent_history"], 1)

    def test_noecho_prompt_inputs_remain_literal_recent_facts(self):
        prompt = view(screen("What do you want to drop? [a-z or ?*]"), (0, 38))
        executor, _ = self.executor([prompt])
        for digit in "12":
            executor.run(Action("input:%02x" % ord(digit), "Type quantity digit " + digit, "input", digit),
                         fingerprint(executor.term.view()))
        events = executor.observer.observation(executor.term.view())["recent_actions"]
        self.assertEqual([event["keys"] for event in events], ["1", "2"])
        self.assertEqual([event["description"] for event in events], ["Type quantity digit 1", "Type quantity digit 2"])
        self.assertEqual(executor.term.sent, ["1", "2"])

    def test_exploration_follows_new_corridor_around_a_corner_within_bound(self):
        tiles = {(2, 3): "#", (2, 4): "#"}
        frames = [corridor((2, 3), tiles)]
        for turn, hero, new in ((401, (2, 4), (3, 4)), (402, (3, 4), (4, 4)),
                                (403, (4, 4), (5, 4))):
            tiles[new] = "#"
            frames.append(corridor(hero, tiles, turn))
        executor, _ = self.executor(frames)
        result = executor.run(Action("explore:3,5", "Explore corridor", "explore", steps=3,
                                     target=(2, 4), route=((2, 4),)), fingerprint(executor.term.view()))
        self.assertEqual(executor.term.sent, ["ml", "mj", "mj"])
        self.assertEqual((result["reason"], result["elapsed_turns"]), ("step_limit", 3))
        self.assertEqual(result["position"], [5, 5])
        self.assertIn({"position": [6, 5], "glyph": "#"}, result["new_terrain"])

    def test_exploration_returns_at_branch_without_choosing_one(self):
        initial = {(2, 3): "#", (2, 4): "#"}
        junction = dict(initial)
        junction.update({(1, 4): "#", (3, 4): "#"})
        executor, _ = self.executor([corridor((2, 3), initial), corridor((2, 4), junction, 401)])
        result = executor.run(Action("explore:3,5", "Explore corridor", "explore", steps=8,
                                     target=(2, 4), route=((2, 4),)), fingerprint(executor.term.view()))
        self.assertEqual(executor.term.sent, ["ml"])
        self.assertEqual(result["reason"], "branch_discovered")

    def test_exploration_returns_on_feature_encounter_or_damage(self):
        initial = {(2, 3): "#", (2, 4): "#"}
        for glyph, hp, reason in (("+", 12, "feature_discovered"), (".", 12, "feature_discovered"),
                                   ("d", 12, "observation_changed"), ("#", 10, "observation_changed")):
            with self.subTest(glyph=glyph, hp=hp):
                revealed = dict(initial)
                revealed[(2, 5)] = glyph
                executor, _ = self.executor([corridor((2, 3), initial), corridor((2, 4), revealed, 401, hp)])
                result = executor.run(Action("explore:3,5", "Explore corridor", "explore", steps=8,
                                             target=(2, 4), route=((2, 4),)), fingerprint(executor.term.view()))
                self.assertEqual(executor.term.sent, ["ml"])
                self.assertEqual(result["reason"], reason)

    def test_exploration_stops_at_new_structural_terrain_colour(self):
        initial = {(2, 3): "#", (2, 4): "#"}
        for colour in ("green", "cyan"):
            for previously_seen in (False, True):
                for kind in ("explore", "travel"):
                    with self.subTest(colour=colour, previously_seen=previously_seen, kind=kind):
                        before = dict(initial)
                        if previously_seen:
                            before[(2, 5)] = "#"
                        revealed = dict(initial)
                        revealed[(2, 5)] = "#"
                        after = corridor((2, 4), revealed, 401)
                        after.fgs[2][5] = colour
                        executor, _ = self.executor([corridor((2, 3), before), after,
                                                     corridor((2, 5), revealed, 402)])
                        result = executor.run(Action(kind + ":3,6", "Go east", kind, steps=8,
                                                     target=(2, 5), route=((2, 4), (2, 5))),
                                              fingerprint(executor.term.view()))
                        self.assertEqual(executor.term.sent, ["ml"])
                        self.assertEqual(result["reason"], "feature_discovered" if kind == "explore" else "observation_changed")
                        self.assertEqual(executor.observer.current.structural_obstacles()[0]["position"], [3, 6])

    def test_search_inside_engulfment_does_not_credit_dungeon_searches(self):
        initial = view(screen("You are swallowed!", ["  /-\\", "  |@|", "  \\-/"]))
        after = view(screen("You are swallowed!", ["  /-\\", "  |@|", "  \\-/"],
                            status2="Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:401"))
        executor, _ = self.executor([initial, after])
        result = executor.run(Action("search:1", "Search", "search", "s"), fingerprint(initial))
        self.assertEqual(executor.term.sent, ["s"])
        self.assertEqual(result["steps"], 1)
        self.assertEqual(executor.observer.current.searches, {})

    def test_new_terrain_returns_control_before_the_next_step(self):
        revealed = room(4, turn=401)
        revealed.rows[4] = "    .".ljust(80)
        executor, _ = self.executor([room(3), revealed, room(5, turn=402)])
        result = executor.run(Action("travel:3,6", "Go east", "travel", steps=2,
                                     route=((2, 4), (2, 5))), fingerprint(executor.term.view()))
        self.assertEqual(result["reason"], "observation_changed")
        self.assertEqual(executor.term.sent, ["ml"])

    def test_menu_pages_are_observed_and_closed_within_query(self):
        menu1 = view(screen("", [" Weapons", " a - a dagger", " (1 of 2)"]), (3, 10))
        menu2 = view(screen("", [" Comestibles", " b - a food ration", " (2 of 2)"]), (3, 10))
        executor, records = self.executor([room(), menu1, menu2, room()])
        result = executor.run(Action("inventory", "Inspect inventory", "inventory", "i"), fingerprint(executor.term.view()))
        self.assertEqual(executor.term.sent, ["i", ">", "\x1b"])
        self.assertEqual(executor.observer.inventory, {"a": "a dagger", "b": "a food ration"})
        self.assertEqual([r["source"] for r in records], ["action", "protocol", "protocol"])
        self.assertEqual(result["steps"], 1)

    def test_full_height_inventory_includes_bottom_item_and_closes(self):
        rows = ["Weapons".ljust(80)] + ["".ljust(80)] * 21 + [" o - an uncursed bell".ljust(80), " (end)".ljust(80)]
        menu = view(rows, (23, 6))
        executor, _ = self.executor([room(), menu, room()])
        result = executor.run(Action("inventory", "Inspect inventory", "inventory", "i"), fingerprint(executor.term.view()))
        self.assertEqual(executor.term.sent, ["i", "\x1b"])
        self.assertEqual(executor.observer.inventory["o"], "an uncursed bell")
        self.assertEqual(result["reason"], "completed")

    def test_prompt_and_death_end_a_bounded_action(self):
        for frame, reason in ((view(screen("Really attack the gnome? [yn] (n)"), (0, 40)), "prompt"),
                              (view(screen("Goodbye Hero the Wizard...--More--"), (0, 20)), "game_over")):
            executor, _ = self.executor([room(), frame, room()])
            result = executor.run(Action("wait:8", "Wait", "wait", ".", 8), fingerprint(executor.term.view()))
            self.assertEqual(result["reason"], reason)
            self.assertEqual(executor.term.sent, ["."])

    def test_caller_can_interrupt_between_steps(self):
        executor, _ = self.executor([room(), room(turn=401), room(turn=402)])
        result = executor.run(Action("wait:8", "Wait", "wait", ".", 8), fingerprint(executor.term.view()),
                              cancelled=lambda: bool(executor.term.sent))
        self.assertEqual(result["reason"], "caller_interrupt")
        self.assertEqual(executor.term.sent, ["."])

    def test_caller_can_interrupt_information_pagination(self):
        for frame in (view(screen("Information.--More--"), (0, 20)),
                      view(screen("", [" Weapons", " a - a dagger", " (1 of 2)"]), (3, 10))):
            with self.subTest(phase=frame.msg):
                executor, records = self.executor([room(), frame, room()])
                result = executor.run(Action("inventory", "Inspect inventory", "inventory", "i"),
                                      fingerprint(executor.term.view()),
                                      cancelled=lambda: bool(executor.term.sent))
                self.assertEqual(result["reason"], "caller_interrupt")
                self.assertEqual(result["steps"], 1)
                self.assertEqual(executor.term.sent, ["i"])
                self.assertEqual(len(records), 1)
                self.assertEqual(len(result["frames"]), 1)
