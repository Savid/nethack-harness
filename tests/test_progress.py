from unittest import TestCase
from nethack_harness.actions import Action
from nethack_harness.progress import NavigationProgress, ObjectiveProgress


class ObjectiveProgressTest(TestCase):
    def test_truncated_history_preserves_total_budget_and_changed_objective_starts_fresh(self):
        progress = ObjectiveProgress()
        observation = {"fingerprint": "observed-screen", "phase": "play",
                       "hero": {"position": [3, 4], "turn": 20}}
        progress.sync(observation, "First task")
        initial = progress.snapshot(40)
        for number in range(40):
            progress.begin(number, "engine", Action("move:l", "East", "move", "l"), observation)
        state = progress.snapshot(40)
        self.assertLess(len(state["recent_attempts"]), 40)
        self.assertEqual(len(state["recent_attempts"]) + state["omitted_attempts"], 40)
        self.assertEqual(state["attempts_remaining"], 0)
        self.assertTrue(progress.exhausted(40))
        self.assertEqual(initial["attempts_used"], 0)
        self.assertEqual(initial["recent_attempts"], [])
        progress.sync(observation, "Another task")
        fresh = progress.snapshot(40)
        self.assertNotEqual(fresh["id"], initial["id"])
        self.assertEqual(fresh["attempts_remaining"], 40)
        self.assertEqual(fresh["recent_attempts"], [])


class NavigationProgressTest(TestCase):
    def test_longer_cycle_is_detected_but_one_return_is_not(self):
        progress = NavigationProgress()

        def observation(p):
            return {"phase": "play", "level": {"id": "level-1", "known_terrain": ["...."]},
                    "hero": {"position": p, "turn": 20}}

        positions = [[3, 4], [3, 5], [4, 5], [3, 4], [3, 5], [4, 5], [3, 4]]
        progress.sync(observation(positions[0]), "Explore")
        for index, (start, end) in enumerate(zip(positions, positions[1:])):
            result = progress.record(observation(start), observation(end), "Explore", Action("move", "Move", "move"),
                                     {"steps": 1, "action": {"target": end}}, index)
            if index < 5:
                self.assertIsNone(result)
        self.assertEqual(result["cycle"], positions[:4])

    def test_nonmovement_action_breaks_a_navigation_sequence(self):
        progress = NavigationProgress()

        def observation(p):
            return {"phase": "play", "level": {"id": "level-1", "known_terrain": ["...."]},
                    "hero": {"position": p}}

        a, b = [3, 4], [3, 5]
        steps = [(a, b, "move"), (b, a, "move"), (a, a, "search"),
                 (a, b, "move"), (b, a, "move"), (a, b, "move"), (b, a, "move")]
        progress.sync(observation(a), "Explore")
        for index, (start, end, kind) in enumerate(steps):
            result = progress.record(observation(start), observation(end), "Explore", Action(kind, kind, kind),
                                     {"steps": 1, "action": {"target": end}}, index)
            if index < 6:
                self.assertIsNone(result)
        self.assertEqual([event["decision"] for event in result["actions"]], [3, 4, 5, 6])
