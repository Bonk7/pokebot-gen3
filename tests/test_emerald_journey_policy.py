"""Story recognition and map routing scenarios without a ROM or emulator."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from modules.emerald_journey import (
    EmeraldStoryPolicy,
    JourneySettings,
    MapEdge,
    StoryFacts,
    StoryGoal,
    next_navigation_goal,
)
from modules.shiny_team_agent import Destination


def facts(**changes):
    initial = StoryFacts(
        "overworld",
        map=(0, 9),
        flags=frozenset({"SET_WALL_CLOCK"}),
        variables=(("LITTLEROOT_INTRO_STATE", 7), ("LITTLEROOT_RIVAL_STATE", 3), ("ROUTE101_STATE", 2)),
    )
    return replace(initial, **changes)


class TestEmeraldStoryPolicy(unittest.TestCase):
    def setUp(self):
        self.policy = EmeraldStoryPolicy(JourneySettings())

    def test_recognises_new_game_ui_states_before_accessing_story_milestones(self):
        for screen, goal in (
            ("boot", StoryGoal.BOOT),
            ("intro", StoryGoal.INTRO),
            ("naming", StoryGoal.NAME),
            ("clock", StoryGoal.CLOCK_UI),
            ("starter_choice", StoryGoal.CHOOSE_STARTER),
        ):
            with self.subTest(screen=screen):
                self.assertEqual(goal, self.policy.decide(StoryFacts(screen)).goal)

    def test_new_game_milestones_are_derived_from_actual_flags(self):
        states = [
            (facts(map=(25, 40), flags=frozenset()), StoryGoal.TRUCK),
            (facts(flags=frozenset()), StoryGoal.CLOCK),
            (facts(variables=(("LITTLEROOT_INTRO_STATE", 6),)), StoryGoal.MOVING_IN),
            (facts(variables=(("LITTLEROOT_INTRO_STATE", 7), ("LITTLEROOT_RIVAL_STATE", 2))), StoryGoal.MEET_RIVAL),
            (facts(variables=(("LITTLEROOT_INTRO_STATE", 7), ("LITTLEROOT_RIVAL_STATE", 3))), StoryGoal.RESCUE),
            (facts(), StoryGoal.STARTER),
            (facts(has_starter=True, starter_level=5, starter_shiny=True), StoryGoal.TRAIN),
            (facts(has_starter=True, starter_level=7, starter_shiny=True), StoryGoal.RIVAL),
            (
                facts(has_starter=True, flags=frozenset({"SET_WALL_CLOCK", "DEFEATED_RIVAL_ROUTE103"})),
                StoryGoal.POKEDEX,
            ),
            (facts(flags=frozenset({"SYS_POKEDEX_GET"})), StoryGoal.SHOES),
            (facts(flags=frozenset({"SYS_POKEDEX_GET", "RECEIVED_RUNNING_SHOES"})), StoryGoal.TEAM),
        ]
        for state, goal in states:
            with self.subTest(goal=goal):
                self.assertEqual(goal, self.policy.decide(state).goal)

    def test_later_save_does_not_replay_missing_intro_variables(self):
        state = StoryFacts(
            "overworld",
            map=(0, 0),
            variables=(),
            flags=frozenset({"SYS_POKEDEX_GET", "RECEIVED_RUNNING_SHOES", "BADGE01_GET"}),
        )
        self.assertEqual(StoryGoal.TEAM, self.policy.decide(state).goal)

    def test_loading_an_older_save_recomputes_the_goal(self):
        later = facts(flags=frozenset({"SYS_POKEDEX_GET", "RECEIVED_RUNNING_SHOES"}))
        self.assertEqual(StoryGoal.TEAM, self.policy.decide(later).goal)
        self.assertEqual(StoryGoal.CLOCK, self.policy.decide(facts(flags=frozenset())).goal)

    def test_incomplete_battle_or_dialogue_cannot_unlock_story_progress(self):
        self.assertEqual(StoryGoal.STARTER_BATTLE, self.policy.decide(facts(screen="battle", first_battle=True)).goal)
        self.assertEqual(StoryGoal.WAIT, self.policy.decide(facts(screen="battle")).goal)
        self.assertEqual(StoryGoal.DIALOGUE, self.policy.decide(facts(screen="dialogue")).goal)

    def test_health_interrupts_story_and_recovery_resumes_goal(self):
        damaged = facts(has_starter=True, starter_level=7, needs_healing=True)
        self.assertEqual(StoryGoal.HEAL, self.policy.decide(damaged).goal)
        self.assertEqual(StoryGoal.RIVAL, self.policy.decide(replace(damaged, needs_healing=False)).goal)

    def test_shiny_starter_is_saved_before_further_story_actions(self):
        state = facts(has_starter=True, starter_shiny=True, starter_level=5, starter_saved=False)
        self.assertEqual(StoryGoal.SAVE_STARTER, self.policy.decide(state).goal)
        self.assertEqual(StoryGoal.TRAIN, self.policy.decide(replace(state, starter_saved=True)).goal)

    def test_rival_defeat_is_not_inferred_from_level_or_location(self):
        state = facts(has_starter=True, starter_level=15, map=(0, 18))
        self.assertEqual(StoryGoal.RIVAL, self.policy.decide(state).goal)
        self.assertEqual(
            StoryGoal.POKEDEX, self.policy.decide(replace(state, flags=state.flags | {"DEFEATED_RIVAL_ROUTE103"})).goal
        )

    def test_unknown_screen_pauses_instead_of_spamming_buttons(self):
        self.assertEqual(StoryGoal.PAUSE, self.policy.decide(StoryFacts("unknown")).goal)


class TestLiveMapRouting(unittest.TestCase):
    def setUp(self):
        self.edges = (
            MapEdge((1, 1), (1, 0), (7, 1)),
            MapEdge((1, 0), (0, 9), (8, 8)),
            MapEdge((0, 9), (0, 16)),
            MapEdge((0, 16), (0, 10)),
            MapEdge((0, 10), (2, 4), (14, 6)),
        )

    def test_routes_through_stairs_and_door_before_connected_routes(self):
        target = Destination((0, 16), (7, 15))
        self.assertEqual(Destination((1, 1), (7, 1)), next_navigation_goal((1, 1), target, self.edges))
        self.assertEqual(Destination((1, 0), (8, 8)), next_navigation_goal((1, 0), target, self.edges))
        self.assertEqual(target, next_navigation_goal((0, 9), target, self.edges))

    def test_route_is_recomputed_from_current_map_after_reload(self):
        target = Destination((2, 4), (3, 3))
        self.assertEqual(Destination((0, 10), (14, 6)), next_navigation_goal((0, 9), target, self.edges))
        self.assertEqual(target, next_navigation_goal((2, 4), target, self.edges))

    def test_unknown_map_has_no_fabricated_teleport_route(self):
        with self.assertRaises(ValueError):
            next_navigation_goal((9, 9), Destination((0, 9), (1, 1)), self.edges)

    def test_cycles_do_not_loop_forever(self):
        edges = self.edges + (MapEdge((0, 16), (0, 9)),)
        with self.assertRaises(ValueError):
            next_navigation_goal((0, 9), Destination((9, 9), (1, 1)), edges)


class TestJourneySettings(unittest.TestCase):
    def test_defaults_and_valid_character_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "emerald_journey.json"
            self.assertEqual(JourneySettings(), JourneySettings.load(path))
            path.write_text(json.dumps({"player_name": "MAY", "gender": "female", "starter": "Treecko"}))
            self.assertEqual("female", JourneySettings.load(path).gender)

    def test_invalid_settings_fail_before_gameplay(self):
        cases = [
            [],
            {"unknown": True},
            {"player_name": "TOO_LONG"},
            {"player_name": ""},
            {"gender": "unknown"},
            {"starter": "Pikachu"},
            {"rival_level": True},
            {"shiny_starter": "yes"},
            {"player_name": "é"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "emerald_journey.json"
            for data in cases:
                with self.subTest(data=data), self.assertRaises(ValueError):
                    path.write_text(json.dumps(data))
                    JourneySettings.load(path)
