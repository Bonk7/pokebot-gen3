"""Policy scenarios run without a ROM, mGBA, GUI or third-party packages."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from modules.shiny_team_agent import (
    Action,
    AgentSettings,
    Destination,
    HuntOption,
    Member,
    Observation,
    ShinyTeamPolicy,
    write_status,
)

SPOT = Destination((0, 16), (10, 10))
HUNT = HuntOption(SPOT, (("Zigzagoon", ("Normal",), 0.5), ("Wurmple", ("Bug",), 0.5)))


def shiny(identity="1", species="Zigzagoon", in_party=True, **kwargs):
    return Member(identity, species, True, in_party, **kwargs)


def observation(**kwargs):
    state = Observation((), 30, True, False, True, True, False, (HUNT,))
    return replace(state, **kwargs)


class TestShinyTeamPolicy(unittest.TestCase):
    def test_hunt_then_heal_then_resume_then_complete(self):
        policy = ShinyTeamPolicy(AgentSettings(team_size=1))
        states = [
            observation(),
            observation(needs_healing=True),
            observation(),
            observation(members=(shiny(),), balls=0),
        ]
        self.assertEqual(
            [Action.HUNT, Action.HEAL, Action.HUNT, Action.COMPLETE], [policy.decide(state).action for state in states]
        )

    def test_boxed_shiny_requires_assembly_before_completion(self):
        policy = ShinyTeamPolicy(AgentSettings(team_size=1))
        self.assertEqual(Action.ASSEMBLE, policy.decide(observation(members=(shiny(in_party=False),))).action)
        self.assertEqual(Action.COMPLETE, policy.decide(observation(members=(shiny(),))).action)

    def test_boxed_shiny_without_reachable_pc_pauses(self):
        policy = ShinyTeamPolicy(AgentSettings(team_size=1))
        self.assertEqual(
            Action.PAUSE, policy.decide(observation(members=(shiny(in_party=False),), can_assemble=False)).action
        )

    def test_duplicate_identity_and_eggs_do_not_fill_team_slots(self):
        policy = ShinyTeamPolicy(AgentSettings(unique_species=False))
        roster = policy.roster(
            (shiny(), shiny(in_party=False), shiny("2", egg=True), Member("3", "Poochyena", False, True))
        )
        self.assertEqual((shiny(),), roster)

    def test_duplicate_species_do_not_fill_distinct_slots(self):
        policy = ShinyTeamPolicy(AgentSettings())
        self.assertEqual(1, len(policy.roster((shiny(), shiny("2")))))

    def test_duplicate_species_can_be_allowed(self):
        policy = ShinyTeamPolicy(AgentSettings(team_size=2, unique_species=False))
        self.assertEqual(Action.COMPLETE, policy.decide(observation(members=(shiny(), shiny("2")))).action)

    def test_party_members_are_preferred_over_same_species_in_pc(self):
        policy = ShinyTeamPolicy(AgentSettings())
        self.assertEqual((shiny(),), policy.roster((shiny("2", in_party=False), shiny())))

    def test_only_target_species_count(self):
        policy = ShinyTeamPolicy(AgentSettings(team_size=1, target_species=("Wurmple",)))
        self.assertEqual((), policy.roster((shiny(),)))
        self.assertEqual(Action.COMPLETE, policy.decide(observation(members=(shiny(species="Wurmple"),))).action)

    def test_failed_or_unverified_catch_never_advances_goal(self):
        policy = ShinyTeamPolicy(AgentSettings(team_size=1))
        self.assertEqual(Action.HUNT, policy.decide(observation()).action)
        self.assertEqual(Action.HUNT, policy.decide(observation()).action)

    def test_loaded_save_recomputes_roster_from_game_facts(self):
        policy = ShinyTeamPolicy(AgentSettings(team_size=1))
        self.assertEqual(Action.COMPLETE, policy.decide(observation(members=(shiny(),))).action)
        self.assertEqual(Action.HUNT, policy.decide(observation()).action)

    def test_low_balls_restock_or_pause_and_never_hunt(self):
        policy = ShinyTeamPolicy(AgentSettings())
        self.assertEqual(Action.RESTOCK, policy.decide(observation(balls=2, can_restock=True)).action)
        self.assertEqual(Action.PAUSE, policy.decide(observation(balls=2)).action)

    def test_healing_without_reachable_center_pauses(self):
        policy = ShinyTeamPolicy(AgentSettings())
        self.assertEqual(Action.PAUSE, policy.decide(observation(needs_healing=True, can_heal=False)).action)

    def test_full_storage_and_no_party_or_hunt_spot_pause(self):
        policy = ShinyTeamPolicy(AgentSettings())
        for changes in ({"can_receive": False}, {"has_party": False}, {"hunts": ()}):
            with self.subTest(changes=changes):
                self.assertEqual(Action.PAUSE, policy.decide(observation(**changes)).action)

    def test_agent_switches_to_a_spot_with_missing_species(self):
        policy = ShinyTeamPolicy(AgentSettings(team_size=2))
        elsewhere = Destination((0, 17), (5, 5))
        duplicate_only = HuntOption(SPOT, (("Zigzagoon", ("Normal",), 1.0),))
        useful = HuntOption(elsewhere, (("Wurmple", ("Bug",), 1.0),), 25)
        decision = policy.decide(observation(members=(shiny(),), hunts=(duplicate_only, useful)))
        self.assertEqual(elsewhere, decision.destination)

    def test_type_coverage_breaks_equal_probability_and_distance_ties(self):
        policy = ShinyTeamPolicy(AgentSettings())
        same_type = HuntOption(SPOT, (("Poochyena", ("Normal",), 1.0),))
        new_type = HuntOption(Destination((0, 17), (1, 1)), (("Wurmple", ("Bug",), 1.0),))
        self.assertEqual(
            new_type.destination,
            policy.decide(observation(members=(shiny(types=("Normal",)),), hunts=(same_type, new_type))).destination,
        )

    def test_hunt_score_accounts_for_distance_and_target_probability(self):
        policy = ShinyTeamPolicy(AgentSettings(team_size=1, target_species=("Wurmple",)))
        far = HuntOption(Destination((0, 17), (1, 1)), (("Wurmple", ("Bug",), 1.0),), 1000)
        self.assertEqual(SPOT, policy.decide(observation(hunts=(HUNT, far))).destination)


class TestAgentConfiguration(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "shiny_team_agent.json"

    def load(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")
        return AgentSettings.load(self.path)

    def test_missing_configuration_uses_six_distinct_species(self):
        self.assertEqual(AgentSettings(), AgentSettings.load(self.path))

    def test_nested_locations_and_shop(self):
        settings = self.load(
            {
                "team_size": 1,
                "hunt_spots": [{"map": [0, 16], "coordinates": [10, 10]}],
                "shop": {
                    "map": [1, 1],
                    "coordinates": [2, 3],
                    "facing": "Up",
                    "entrance": {"map": [0, 1], "coordinates": [4, 5]},
                },
            }
        )
        self.assertEqual((SPOT,), settings.hunt_spots)
        self.assertEqual("Poké Ball", settings.shop.ball)
        self.assertEqual(Destination((0, 1), (4, 5)), settings.shop.entrance)

    def test_rejects_malformed_and_unsafe_configuration(self):
        cases = [
            [],
            {"team_size": 7},
            {"team_size": True},
            {"heal_hp_fraction": float("nan")},
            {"heal_hp_fraction": True},
            {"decision_interval": 0},
            {"unknown": 1},
            {"unique_species": "yes"},
            {"target_species": ["Zigzagoon"]},
            {"target_species": ["A", "A"]},
            {"hunt_spots": [{"map": [1], "coordinates": [1, 2]}]},
            {"shop": {"map": [1, 2], "coordinates": [-1, 2], "facing": "Up"}},
            {"shop": {"map": [1, 2], "coordinates": [1, 2], "facing": "Up", "target_balls": 2}},
        ]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.load(data)

    def test_status_report_replaces_stale_roster_atomically(self):
        policy = ShinyTeamPolicy(AgentSettings(team_size=1))
        write_status(self.path, policy.decide(observation(members=(shiny(),))), (shiny(),), 1)
        write_status(self.path, policy.decide(observation()), (), 1)
        data = json.loads(self.path.read_text())
        self.assertEqual("hunt", data["action"])
        self.assertEqual([], data["roster"])
        self.assertFalse(self.path.with_suffix(".tmp").exists())
