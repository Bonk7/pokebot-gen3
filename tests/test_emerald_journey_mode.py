"""Opening-game adapter contracts without requiring or distributing a ROM."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.emerald_journey import StoryFacts, StoryGoal


def one_frame(*args, **kwargs):
    yield


class TestEmeraldJourneyMode(unittest.TestCase):
    def setUp(self):
        from modules.modes import emerald_journey as adapter
        from modules.modes import shiny_team_agent as team_adapter

        self.adapter = adapter
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.context = SimpleNamespace(
            profile=SimpleNamespace(path=Path(directory.name)),
            emulator=MagicMock(),
            bot_mode="Emerald Journey Agent",
            message="",
            rom=SimpleNamespace(is_emerald=True, language=adapter.ROMLanguage.English, game_name="Pokémon Emerald"),
        )
        for module in (adapter, team_adapter):
            patcher = patch.object(module, "context", self.context)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.mode = adapter.EmeraldJourneyMode()

    def patch(self, name, **kwargs):
        patcher = patch.object(self.adapter, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def test_journey_can_be_selected_before_new_game(self):
        self.assertTrue(self.mode.can_start_at_title)
        self.assertTrue(self.mode.is_selectable())
        self.context.rom.language = self.adapter.ROMLanguage.German
        self.assertFalse(self.mode.is_selectable())

    def test_cli_preselects_journey_before_a_new_profile_is_created(self):
        import pokebot

        with patch("sys.argv", ["pokebot.py", "--bot-mode", "Emerald Journey Agent"]):
            settings = pokebot.parse_arguments(["Manual", "Emerald Journey Agent"])
        self.assertIsNone(settings.profile)
        self.assertEqual("Emerald Journey Agent", settings.bot_mode)

    def test_boot_selects_top_item_instead_of_overwriting_existing_save(self):
        self.patch("get_task", return_value=SimpleNamespace(data_value=lambda index: 1))
        list(self.mode._execute_story(StoryGoal.BOOT, StoryFacts("boot")))
        self.context.emulator.press_button.assert_called_with("Up")
        self.patch("get_task", return_value=SimpleNamespace(data_value=lambda index: 0))
        list(self.mode._execute_story(StoryGoal.BOOT, StoryFacts("boot")))
        self.context.emulator.press_button.assert_called_with("A")

    def test_birch_intro_is_recognised_even_while_main_callback_is_menu(self):
        self.patch("get_game_state", return_value=self.adapter.GameState.MAIN_MENU)
        self.patch("get_game_state_symbol", return_value="CB2_MAINMENU")
        self.patch("get_tasks", return_value=[SimpleNamespace(symbol="Task_NewGameBirchSpeech_ChooseGender")])
        self.assertEqual("intro", self.mode._story_facts().screen)

    def test_starter_choice_is_not_mistaken_for_an_unknown_screen(self):
        self.patch("get_game_state", return_value=self.adapter.GameState.CHOOSE_STARTER)
        self.patch("get_game_state_symbol", return_value="CB2_CHOOSESTARTER")
        self.patch("get_tasks", return_value=[])
        self.assertEqual("starter_choice", self.mode._story_facts().screen)

    def test_observer_reads_later_story_flags_without_requiring_an_intro_counter(self):
        self.patch("get_game_state", return_value=self.adapter.GameState.OVERWORLD)
        self.patch("get_game_state_symbol", return_value="CB2_OVERWORLD")
        self.patch("get_tasks", return_value=[])
        self.patch("game_has_started", return_value=True)
        self.patch("get_global_script_context", return_value=SimpleNamespace(is_active=False))
        self.patch("get_party", return_value=SimpleNamespace(non_eggs=[]))
        self.patch("get_event_flag", side_effect=lambda name: name in {"SYS_POKEDEX_GET", "RECEIVED_RUNNING_SHOES"})
        self.patch("get_event_var", return_value=0)
        self.patch(
            "get_map_data_for_current_position",
            return_value=SimpleNamespace(map_group_and_number=(0, 9), local_position=(10, 2)),
        )
        self.patch("player_avatar_is_controllable", return_value=True)
        self.patch("get_player", return_value=SimpleNamespace(gender="female"))
        observed = self.mode._story_facts()
        self.assertEqual("female", observed.gender)
        self.assertEqual(StoryGoal.TEAM, self.mode.story_policy.decide(observed).goal)

    def test_player_naming_uses_configured_name_and_length(self):
        keyboard = self.patch("type_in_naming_screen", side_effect=one_frame)
        list(self.mode._execute_story(StoryGoal.NAME, StoryFacts("naming")))
        keyboard.assert_called_once_with("BOT", max_length=7)

    def test_first_battle_is_owned_starter_not_a_catch_target(self):
        encounter = SimpleNamespace(type=self.adapter.EncounterType.Gift)
        self.assertEqual(self.adapter.BattleAction.CustomAction, self.mode.on_battle_started(encounter))
        self.assertIs(encounter, self.mode._starter_encounter)

    def test_nonshiny_starter_never_resets_without_verified_cartridge_checkpoint(self):
        self.patch("get_main_battle_callback", return_value="HandleTurnActionSelectionState")
        self.patch("get_party", return_value=SimpleNamespace(non_eggs=[SimpleNamespace(is_shiny=False)]))
        reset = self.patch("soft_reset", side_effect=one_frame)
        self.mode._starter_checkpoint_is_valid = lambda: False
        with self.assertRaises(self.adapter.BotModeError):
            list(self.mode._evaluate_starter())
        reset.assert_not_called()

    def test_nonshiny_starter_retries_from_verified_checkpoint(self):
        self.patch("get_main_battle_callback", return_value="HandleTurnActionSelectionState")
        starter = SimpleNamespace(is_shiny=False, species=SimpleNamespace(name="Mudkip"))
        self.patch("get_party", return_value=SimpleNamespace(non_eggs=[starter]))
        reset = self.patch("soft_reset", side_effect=one_frame)
        self.mode._starter_checkpoint_is_valid = lambda: True
        list(self.mode._evaluate_starter())
        reset.assert_called_once_with(mash_random_keys=True)
        self.context.emulator.create_save_state.assert_not_called()

    def test_shiny_starter_is_kept_and_birch_battle_is_fought(self):
        self.patch("get_main_battle_callback", return_value="HandleTurnActionSelectionState")
        starter = SimpleNamespace(is_shiny=True, species=SimpleNamespace(name="Mudkip"))
        self.patch("get_party", return_value=SimpleNamespace(non_eggs=[starter]))
        reset = self.patch("soft_reset", side_effect=one_frame)
        self.patch("AgentTrainerStrategy", return_value=object())
        fight = self.patch("handle_battle", side_effect=one_frame)
        list(self.mode._evaluate_starter())
        reset.assert_not_called()
        fight.assert_called_once()
        self.context.emulator.create_save_state.assert_called_once_with(suffix="Journey_StarterAccepted")

    def test_confirming_a_starter_waits_for_naturally_fresh_rng(self):
        self.patch("get_task", return_value=None)
        self.patch("task_is_active", return_value=True)
        self.patch("get_rng_state_history", return_value={1})
        self.patch("read_symbol", side_effect=[b"\1\0\0\0", b"\2\0\0\0"])
        history = self.patch("save_rng_state_history", return_value=True)
        list(self.mode._starter_choice())
        history.assert_called_once_with({1, 2})
        self.context.emulator.press_button.assert_called_once_with("A")

    def test_accepted_starter_is_verified_in_cartridge_after_saving(self):
        pokemon = SimpleNamespace(data=b"12345678")
        self.patch("get_party", return_value=SimpleNamespace(non_eggs=[pokemon]))
        self.patch("save_the_game", side_effect=one_frame)
        self.patch("get_save_data", return_value=SimpleNamespace(get_party=lambda: [pokemon]))
        list(self.mode._execute_story(StoryGoal.SAVE_STARTER, StoryFacts("overworld")))
        self.context.emulator.create_save_state.assert_called_once_with(suffix="Journey_StarterCartridgeSaved")

    def test_empty_action_still_advances_emulator_frames(self):
        self.mode._story_facts = lambda: StoryFacts("boot")

        def empty_action(*args):
            return
            yield

        self.mode._execute_story = empty_action
        runner = self.mode._run_agent()
        self.addCleanup(runner.close)
        # Without the explicit frame boundary an already reached coordinate goal
        # would spin in Python and time out before the game could run its script.
        next(runner)
        next(runner)

    def test_catching_unlock_hands_off_with_journey_mode_name_and_regional_hunts(self):
        self.mode._story_facts = lambda: StoryFacts(
            "overworld", map=(0, 9), flags=frozenset({"SYS_POKEDEX_GET", "RECEIVED_RUNNING_SHOES"})
        )
        self.mode._grass = lambda address: self.adapter.Destination(address.value, (10, 10))
        child = MagicMock()
        child._known_spots = set()
        child.run.side_effect = one_frame
        factory = self.patch("ShinyTeamAgentMode", return_value=child)
        list(self.mode._run_agent())
        self.assertEqual("Emerald Journey Agent", factory.call_args.kwargs["active_mode_name"])
        self.assertEqual(3, len(child._known_spots))
        self.assertEqual((3, 3), factory.call_args.args[0].shop.destination.coordinates)
        child.run.assert_called_once()

    def test_nickname_is_declined_but_story_agreement_is_accepted(self):
        self.patch("task_is_active", return_value=True)
        self.assertEqual(
            "B", self.mode._dialogue_button(StoryFacts("dialogue", scripts=("EventScript_GiveStarterEvent",)))
        )
        self.assertEqual(
            "A",
            self.mode._dialogue_button(
                StoryFacts("dialogue", scripts=("EventScript_GiveStarterEvent", "EventScript_GoSeeRival"))
            ),
        )

    def test_team_controller_receives_battle_and_whiteout_events_after_handoff(self):
        child = MagicMock()
        self.mode._team_agent = child
        encounter = object()
        self.mode.on_battle_started(encounter)
        self.mode.on_battle_ended(self.adapter.BattleOutcome.Caught)
        self.mode.on_whiteout()
        child.on_battle_started.assert_called_once_with(encounter)
        child.on_battle_ended.assert_called_once_with(self.adapter.BattleOutcome.Caught)
        child.on_whiteout.assert_called_once()
