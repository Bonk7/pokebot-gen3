"""Emulator adapter contracts, with synthetic game facts and no ROM."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.shiny_team_agent import AgentSettings, Destination, HuntOption, Member, Observation, Shop


def one_frame():
    yield


class TestShinyTeamAgentMode(unittest.TestCase):
    def setUp(self):
        from modules.modes import shiny_team_agent as adapter

        self.adapter = adapter
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        (self.path / "shiny_team_agent.json").write_text('{"team_size": 1}')
        self.context = SimpleNamespace(
            profile=SimpleNamespace(path=self.path),
            emulator=MagicMock(),
            bot_mode="Shiny Team Agent",
            message="",
        )
        self.context.set_manual_mode = lambda: setattr(self.context, "bot_mode", "Manual")
        context_patch = patch.object(adapter, "context", self.context)
        context_patch.start()
        self.addCleanup(context_patch.stop)
        self.mode = adapter.ShinyTeamAgentMode()
        self.mode.settings = AgentSettings(team_size=1)
        self.pokemon = SimpleNamespace(
            is_valid=True,
            is_egg=False,
            is_shiny=True,
            personality_value=1234,
            species=SimpleNamespace(name="Zigzagoon"),
            data=bytes(range(8)),
        )
        self.encounter = SimpleNamespace(
            is_shiny=True,
            pokemon=self.pokemon,
            type=adapter.EncounterType.Land,
            battle_action=None,
        )

    def patch(self, name, **kwargs):
        patcher = patch.object(self.adapter, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def test_shiny_is_protected_and_caught_despite_normal_auto_catch_settings(self):
        self.patch("get_party", return_value=[self.pokemon])
        self.patch("get_item_bag", return_value=SimpleNamespace(number_of_balls_except_master_ball=1))
        handler = self.patch("handle_encounter")
        strategy = self.patch("CatchStrategy")
        self.assertIs(strategy.return_value, self.mode.on_battle_started(self.encounter))
        self.assertEqual((1234, "Zigzagoon"), self.mode._pending_capture)
        self.assertEqual(self.adapter.BattleAction.Catch, self.encounter.battle_action)
        handler.assert_called_once_with(self.encounter, do_not_switch_to_manual=True)
        self.context.emulator.create_save_state.assert_called_once_with(suffix="Agent_ShinyEncounter")

    def test_no_balls_preserves_shiny_and_stops_before_catching(self):
        self.patch("get_item_bag", return_value=SimpleNamespace(number_of_balls_except_master_ball=0))
        strategy = self.patch("CatchStrategy")
        with self.assertRaises(self.adapter.BotModeError):
            self.mode.on_battle_started(self.encounter)
        strategy.assert_not_called()
        self.assertIsNone(self.mode._pending_capture)
        self.context.emulator.create_save_state.assert_called_once()

    def test_full_storage_does_not_start_capture(self):
        self.patch("get_item_bag", return_value=SimpleNamespace(number_of_balls_except_master_ball=10))
        self.patch("get_party", return_value=[self.pokemon] * 6)
        self.patch("get_pokemon_storage", return_value=SimpleNamespace(pokemon_count=420))
        with self.assertRaises(self.adapter.BotModeError):
            self.mode.on_battle_started(self.encounter)

    def test_ordinary_wild_pokemon_uses_escape_strategy(self):
        self.encounter.is_shiny = False
        strategy = self.patch("AgentRunAwayStrategy")
        self.assertIs(strategy.return_value, self.mode.on_battle_started(self.encounter))
        self.context.emulator.create_save_state.assert_not_called()

    def test_only_caught_outcome_requests_ownership_verification(self):
        self.mode._pending_capture = (1234, "Zigzagoon")
        self.mode.on_battle_ended(self.adapter.BattleOutcome.Caught)
        self.assertEqual((1234, "Zigzagoon"), self.mode._verify_capture)
        self.assertIsNone(self.mode._pending_capture)

    def test_failed_shiny_capture_requests_pause(self):
        self.mode._pending_capture = (1234, "Zigzagoon")
        self.mode.on_battle_ended(self.adapter.BattleOutcome.Lost)
        self.assertIsNone(self.mode._verify_capture)
        self.assertIn("failed", self.mode._capture_failure)

    def test_capture_is_verified_in_party_or_box_before_checkpoint(self):
        for in_party in (True, False):
            with self.subTest(in_party=in_party):
                self.mode._verify_capture = (1234, "Zigzagoon")
                self.mode._pokemon = lambda: ([self.pokemon], []) if in_party else ([], [self.pokemon])
                self.mode._verify_caught_pokemon()
                self.assertIsNone(self.mode._verify_capture)
        self.assertEqual(2, self.context.emulator.create_save_state.call_count)

    def test_ghost_capture_does_not_checkpoint_or_advance(self):
        self.mode._verify_capture = (1234, "Zigzagoon")
        self.mode._pokemon = lambda: ([], [])
        with self.assertRaises(self.adapter.BotModeError):
            self.mode._verify_caught_pokemon()
        self.assertIsNotNone(self.mode._verify_capture)
        self.context.emulator.create_save_state.assert_not_called()

    def test_wrong_personality_or_nonshiny_does_not_verify(self):
        for changed in (dict(personality_value=999), dict(is_shiny=False), dict(is_valid=False)):
            with self.subTest(changed=changed):
                pokemon = SimpleNamespace(**{**vars(self.pokemon), **changed})
                self.mode._pokemon = lambda: ([pokemon], [])
                self.mode._verify_capture = (1234, "Zigzagoon")
                with self.assertRaises(self.adapter.BotModeError):
                    self.mode._verify_caught_pokemon()

    def test_old_navigation_is_discarded_after_a_verified_catch(self):
        self.mode._verify_capture = (1234, "Zigzagoon")
        self.mode._pokemon = lambda: ([self.pokemon], [])
        movement = MagicMock()

        def action():
            movement()
            yield

        list(self.mode._bounded(action()))
        movement.assert_not_called()
        self.assertIsNone(self.mode._verify_capture)

    def test_action_timeout_closes_controller(self):
        self.mode.settings = AgentSettings(action_timeout=3)
        closed = []

        def endless():
            try:
                while True:
                    yield
            finally:
                closed.append(True)

        with self.assertRaises(self.adapter.BotModeError):
            list(self.mode._bounded(endless()))
        self.assertEqual([True], closed)

    def test_hunting_stops_for_capture_verification_before_more_inputs(self):
        destination = Destination((0, 16), (10, 10))
        self.patch("get_player_location", return_value=(destination.map, destination.coordinates))
        self.mode._verify_capture = (1234, "Zigzagoon")
        self.patch("spin", return_value=one_frame())
        self.assertEqual([], list(self.mode._hunt(destination)))
        self.assertIsNotNone(self.mode._verify_capture)

    def test_shop_route_verifies_door_and_interior_before_navigation(self):
        entrance = Destination((0, 1), (5, 5))
        clerk = Destination((5, 0), (2, 3))
        self.mode.settings = AgentSettings(shop=Shop(clerk, "Up", entrance=entrance))
        self.patch("get_player_location", return_value=((0, 1), (1, 1)))
        door = SimpleNamespace(is_warp=True, map=(5, 0), coordinates=(3, 8))
        path = self.patch("calculate_path", side_effect=[self.adapter.PathFindingError("Disconnected"), [door], []])
        self.assertEqual([entrance, clerk], self.mode._shop_route())
        self.assertEqual(3, path.call_count)

    def test_restock_enters_shops_exits_and_checks_reserve(self):
        entrance = Destination((0, 1), (5, 5))
        clerk = Destination((5, 0), (2, 3))
        self.mode.settings = AgentSettings(shop=Shop(clerk, "Up", entrance=entrance))
        self.mode._shop_route = lambda: [entrance, clerk]
        self.context.rom = SimpleNamespace(is_rs=False)
        bag = SimpleNamespace(number_of_balls_except_master_ball=2, has_space_for=lambda item, amount: True)
        self.patch("get_item_bag", return_value=bag)
        self.patch("get_item_by_name", return_value=SimpleNamespace(price=200))
        self.patch("get_player", return_value=SimpleNamespace(money=10000))
        self.patch("task_is_active", side_effect=[True, False])
        self.patch("ensure_facing_direction", side_effect=lambda _: one_frame())
        self.patch("wait_for_no_script_to_run", side_effect=lambda _: one_frame())
        self.patch("wait_for_player_avatar_to_be_controllable", side_effect=one_frame)
        navigate = self.patch("navigate_to", side_effect=lambda *args: one_frame())

        def buy(shopping_list):
            bag.number_of_balls_except_master_ball += shopping_list[0][1]
            yield

        self.patch("buy_in_shop", side_effect=buy)
        warp = SimpleNamespace(destination_map_group=0, destination_map_number=1, local_coordinates=(3, 8))
        self.patch(
            "get_map_data_for_current_position", return_value=SimpleNamespace(map_group_and_number=(5, 0), warps=[warp])
        )
        self.patch("get_player_location", return_value=((5, 0), (2, 3)))
        self.patch("calculate_path", return_value=[])
        list(self.mode._restock())
        self.assertEqual(50, bag.number_of_balls_except_master_ball)
        self.assertEqual(
            [((entrance.map, entrance.coordinates),), ((clerk.map, clerk.coordinates),), (((5, 0), (3, 8)),)],
            [(call.args,) for call in navigate.call_args_list],
        )

    def test_no_encounter_watchdog_pauses_instead_of_spinning_forever(self):
        self.mode.settings = AgentSettings(decision_interval=10, encounter_timeout=3)
        destination = Destination((0, 16), (10, 10))
        self.patch("get_player_location", return_value=(destination.map, destination.coordinates))

        def endless_spin():
            while True:
                yield

        self.patch("spin", side_effect=endless_spin)
        with self.assertRaises(self.adapter.BotModeError):
            list(self.mode._hunt(destination))

    def test_loop_replans_from_healing_to_hunting_to_ready(self):
        state = self.adapter.GameState.OVERWORLD
        self.patch("get_game_state", return_value=state)
        self.patch("is_safari_map", return_value=False)
        self.patch("player_avatar_is_controllable", return_value=True)
        self.patch("get_player_location", return_value=((0, 16), (10, 10)))
        self.mode.is_selectable = lambda: True
        hunt = HuntOption(Destination((0, 16), (10, 10)), (("Zigzagoon", ("Normal",), 1.0),))
        initial = Observation((), 30, True, True, True, True, False, (hunt,))
        healthy = replace(initial, needs_healing=False)
        ready = replace(healthy, members=(Member("1", "Zigzagoon", True, True),))
        self.mode._observe = MagicMock(side_effect=[initial, healthy, ready])
        self.mode._hunt = MagicMock(side_effect=lambda _: one_frame())
        heal = self.patch("heal_in_pokemon_center", side_effect=lambda _: one_frame())
        list(self.mode.run())
        heal.assert_called_once()
        self.mode._hunt.assert_called_once()
        self.assertEqual("Manual", self.context.bot_mode)
        status = json.loads((self.path / "shiny_team_agent.status.json").read_text())
        self.assertEqual("complete", status["action"])
        self.assertEqual(1, len(status["roster"]))
