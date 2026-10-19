"""New Game to shiny party for English retail Emerald.

Opening-story knowledge is based on pret/pokeemerald at
731ad5bfd6e6f265508d0efcca0ba42f9dcf5881 (main_menu.c, starter_choose.c,
players_house.inc and Littleroot/Route101/Route103 map scripts).
All game changes go through controller input and normal saves/resets.
"""

import json
from dataclasses import asdict, replace
from typing import Generator

from modules.battle_handler import handle_battle
from modules.battle_state import BattleOutcome, BattleType, EncounterType, get_battle_state, get_main_battle_callback
from modules.context import context
from modules.emerald_journey import (
    STORY_FLAGS,
    STORY_VARS,
    EmeraldStoryPolicy,
    JourneySettings,
    MapEdge,
    StoryDecision,
    StoryFacts,
    StoryGoal,
    next_navigation_goal,
)
from modules.files import get_rng_state_history, save_rng_state_history
from modules.keyboard import type_in_naming_screen
from modules.map import get_map_all_tiles, get_map_data, get_map_data_for_current_position
from modules.map_data import MapRSE, PokemonCenter
from modules.memory import (
    GameState,
    game_has_started,
    get_event_flag,
    get_event_var,
    get_game_state,
    get_game_state_symbol,
    read_symbol,
    unpack_uint32,
)
from modules.player import get_player, get_player_location, player_avatar_is_controllable
from modules.pokemon import StatusCondition, pokemon_has_usable_damaging_move
from modules.pokemon_party import get_party
from modules.roms import ROMLanguage
from modules.save_data import get_save_data
from modules.shiny_team_agent import AgentSettings, Destination, Shop
from modules.tasks import get_global_script_context, get_task, get_tasks, task_is_active
from ._asserts import SavedMapLocation, assert_saved_on_map
from ._interface import BattleAction, BotModeError
from .shiny_team_agent import AgentRunAwayStrategy, AgentTrainerStrategy, ShinyTeamAgentMode
from .util import (
    ensure_facing_direction,
    heal_in_pokemon_center,
    navigate_to,
    save_the_game,
    soft_reset,
    talk_to_npc,
)
from .util.pokecenter_loop import PokecenterLoopController

EARLY_MAPS = (
    MapRSE.LITTLEROOT_TOWN,
    MapRSE.OLDALE_TOWN,
    MapRSE.ROUTE101,
    MapRSE.ROUTE102,
    MapRSE.ROUTE103,
    MapRSE.LITTLEROOT_TOWN_BRENDANS_HOUSE_1F,
    MapRSE.LITTLEROOT_TOWN_BRENDANS_HOUSE_2F,
    MapRSE.LITTLEROOT_TOWN_MAYS_HOUSE_1F,
    MapRSE.LITTLEROOT_TOWN_MAYS_HOUSE_2F,
    MapRSE.LITTLEROOT_TOWN_PROFESSOR_BIRCHS_LAB,
    MapRSE.OLDALE_TOWN_POKEMON_CENTER_1F,
    MapRSE.OLDALE_TOWN_MART,
)


class EmeraldJourneyMode(ShinyTeamAgentMode):
    can_start_at_title = True

    @staticmethod
    def name() -> str:
        return "Emerald Journey Agent"

    @staticmethod
    def is_selectable() -> bool:
        return (
            context.rom.is_emerald
            and context.rom.language is ROMLanguage.English
            and not context.rom.game_name.startswith("Unsupported ")
        )

    def __init__(self):
        super().__init__()
        self.journey = JourneySettings()
        self.story_policy = EmeraldStoryPolicy(self.journey)
        self._team_agent: ShinyTeamAgentMode | None = None
        self._training: PokecenterLoopController | None = None
        self._starter_encounter = None
        self._last_story = None

    def _story_facts(self) -> StoryFacts:
        state = get_game_state()
        tasks = tuple(task.symbol.upper() for task in get_tasks())
        callback = get_game_state_symbol().upper()
        if any("NEWGAMEBIRCHSPEECH" in name for name in tasks) or "NEWGAMEBIRCHSPEECH" in callback:
            return StoryFacts("intro")
        if state is GameState.NAMING_SCREEN:
            return StoryFacts("naming")
        if state is GameState.CHOOSE_STARTER:
            return StoryFacts("starter_choice", scripts=tasks)
        if any("SETCLOCK" in name for name in tasks) or "WALLCLOCK" in callback:
            return StoryFacts("clock")
        if state in (GameState.TITLE_SCREEN, GameState.MAIN_MENU):
            return StoryFacts("boot")
        if state in (GameState.CHANGE_MAP, GameState.GARBAGE_COLLECTION) or not game_has_started():
            return StoryFacts("transition")
        script = get_global_script_context()
        scripts = tuple(script.stack) if script.is_active else ()
        if script.is_active:
            scripts += (script.script_function_name,)
        party = get_party().non_eggs
        flags = frozenset(name for name in STORY_FLAGS if get_event_flag(name))
        variables = tuple((name, get_event_var(name)) for name in STORY_VARS)
        in_battle = state in (GameState.BATTLE, GameState.BATTLE_STARTING, GameState.BATTLE_ENDING)
        first_battle = state is GameState.BATTLE and BattleType.FirstBattle in get_battle_state().type
        location = get_map_data_for_current_position()
        if in_battle:
            screen = "battle"
        elif state is GameState.OVERWORLD and (script.is_active or not player_avatar_is_controllable()):
            screen = "dialogue"
        elif state is GameState.OVERWORLD:
            screen = "overworld"
        else:
            screen = "unknown"
        starter_saved = True
        if screen == "overworld" and party and party[0].is_shiny and "RESCUED_BIRCH" in flags:
            saved = get_save_data()
            starter_saved = saved is not None and any(p.data[:8] == party[0].data[:8] for p in saved.get_party())
        return StoryFacts(
            screen,
            location.map_group_and_number,
            location.local_position,
            get_player().gender,
            flags,
            variables,
            scripts,
            bool(party),
            party[0].level if party else 0,
            party[0].is_shiny if party else False,
            any(
                p.current_hp / max(1, p.stats.hp) < self.settings.heal_hp_fraction
                or p.status_condition is not StatusCondition.Healthy
                for p in party
            )
            or bool(party)
            and not any(pokemon_has_usable_damaging_move(p) for p in party if p.current_hp > 0),
            first_battle,
            starter_saved,
        )

    def _report_story(self, decision: StoryDecision, facts: StoryFacts) -> None:
        context.message = decision.reason
        signature = (decision.goal, facts)
        if signature == self._last_story:
            return
        data = {
            "goal": decision.goal.value,
            "reason": decision.reason,
            "facts": {**asdict(facts), "flags": sorted(facts.flags)},
        }
        path = context.profile.path / "emerald_journey.status.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
        if self._last_story is None or self._last_story[0] != decision.goal:
            self._event("story_goal", goal=decision.goal.value, reason=decision.reason)
        self._last_story = signature

    def _map_edges(self) -> tuple[MapEdge, ...]:
        edges = []
        for address in EARLY_MAPS:
            location = get_map_data(address, (0, 0))
            for connection in location.connections:
                if connection.direction in ("North", "South", "East", "West"):
                    edges.append(
                        MapEdge(address.value, (connection.destination_map_group, connection.destination_map_number))
                    )
            for warp in location.warps:
                edges.append(
                    MapEdge(
                        address.value, (warp.destination_map_group, warp.destination_map_number), warp.local_coordinates
                    )
                )
        return tuple(edges)

    def _travel(self, destination: Destination) -> Generator:
        location, _ = get_player_location()
        try:
            step = next_navigation_goal(tuple(location), destination, self._map_edges())
        except ValueError as error:
            raise BotModeError(str(error)) from error
        yield from self._bounded(navigate_to(step.map, step.coordinates, run=False, expecting_script=True))

    def _interact(self, destination: Destination, facing: str | None = None, npc: int | None = None) -> Generator:
        current_map, _ = get_player_location()
        if current_map != destination.map:
            yield from self._travel(destination)
            return
        if npc is not None:
            yield from self._bounded(talk_to_npc(npc))
            return
        yield from self._travel(destination)
        if not player_avatar_is_controllable():
            return
        if facing:
            yield from ensure_facing_direction(facing)
        context.emulator.press_button("A")
        yield

    @staticmethod
    def _grass(address: MapRSE) -> Destination:
        tiles = [
            tile
            for tile in get_map_all_tiles(get_map_data(address, (0, 0)))
            if tile.has_encounters and not tile.collision and not tile.is_surfable
        ]
        if not tiles:
            raise BotModeError(f"No land encounter spot was found on {address.name}.")
        tile = min(tiles, key=lambda t: abs(t.local_position[0] - 10) + abs(t.local_position[1] - 10))
        return Destination(address.value, tile.local_position)

    def _starter_checkpoint_is_valid(self) -> bool:
        saved = get_save_data()
        if saved is None or len(saved.get_party()) != 0:
            return False
        try:
            assert_saved_on_map([SavedMapLocation(MapRSE.ROUTE101, (7, 14), facing=True)], "Not saved at Birch's bag.")
        except BotModeError:
            return False
        return True

    def _starter_choice(self) -> Generator:
        task = get_task("Task_HandleStarterChooseInput")
        if task is not None:
            desired = ("Treecko", "Torchic", "Mudkip").index(self.journey.starter)
            selected = task.data_value(0)
            context.emulator.press_button("Left" if selected > desired else "Right" if selected < desired else "A")
            yield
            return
        if task_is_active("Task_HandleConfirmStarterInput"):
            # Wait for a naturally occurring RNG state not used on a prior roll.
            # Do not write seeds or change a Pokémon's shiny status.
            history = get_rng_state_history()
            while (value := unpack_uint32(read_symbol("gRngValue"))) in history:
                yield
            history.add(value)
            if not save_rng_state_history(history):
                raise BotModeError("Could not preserve the starter roll history.")
            context.emulator.press_button("A")
        yield

    def _evaluate_starter(self) -> Generator:
        while get_main_battle_callback() != "HandleTurnActionSelectionState":
            if get_game_state() not in (GameState.BATTLE, GameState.BATTLE_STARTING):
                return
            context.emulator.press_button("A")
            yield
        starter = get_party().non_eggs[0]
        if self.journey.shiny_starter and not starter.is_shiny:
            if not self._starter_checkpoint_is_valid():
                raise BotModeError(
                    "The starter is not shiny and no verified pre-starter cartridge save exists. No reset performed."
                )
            self._event("starter_retry", species=starter.species.name)
            self._starter_encounter = None
            yield from soft_reset(mash_random_keys=True)
            return
        context.emulator.create_save_state(suffix="Journey_StarterAccepted")
        self._event("starter_accepted", species=starter.species.name, shiny=starter.is_shiny)
        if self._starter_encounter is not None:
            self._starter_encounter.battle_action = BattleAction.Fight
        yield from handle_battle(AgentTrainerStrategy())
        self._starter_encounter = None

    def on_battle_started(self, encounter):
        if self._team_agent is not None:
            return self._team_agent.on_battle_started(encounter)
        if encounter is not None and encounter.type is EncounterType.Gift:
            self._starter_encounter = encounter
            return BattleAction.CustomAction
        if encounter is None:
            return AgentTrainerStrategy()
        if encounter.is_shiny:
            return super().on_battle_started(encounter)
        if self._training is not None:
            encounter.battle_action = BattleAction.Fight
            return AgentTrainerStrategy()
        encounter.battle_action = BattleAction.RunAway
        return AgentRunAwayStrategy()

    def on_battle_ended(self, outcome: BattleOutcome) -> None:
        if self._team_agent is not None:
            self._team_agent.on_battle_ended(outcome)
        else:
            super().on_battle_ended(outcome)
            if self._training is not None:
                self._training.on_battle_ended()

    def on_whiteout(self) -> bool:
        if self._team_agent is not None:
            return self._team_agent.on_whiteout()
        self._event("story_whiteout")
        if self._training is not None:
            self._training.on_whiteout()
        return True

    def _dialogue_button(self, facts: StoryFacts) -> str:
        scripts = " ".join(facts.scripts).upper()
        if "GOSEERIVAL" in scripts or "DECLINESEEINGRIVAL" in scripts:
            return "A"
        if task_is_active("Task_HandleYesNoInput") and "GIVESTARTER" in scripts:
            return "B"  # Decline the optional starter nickname, then agree to visit the rival.
        return "A"

    def _execute_story(self, goal: StoryGoal, facts: StoryFacts) -> Generator:
        male = facts.gender == "male"
        if goal is StoryGoal.BOOT:
            task = get_task("Task_HandleMainMenuInput")
            # Item zero means New Game for an empty cartridge and Continue for
            # an existing save. Never choose New Game over an existing save.
            context.emulator.press_button("Up" if task is not None and task.data_value(1) != 0 else "A")
            yield
        elif goal is StoryGoal.INTRO:
            if task_is_active("Task_NewGameBirchSpeech_ChooseGender"):
                context.emulator.press_button("Up" if self.journey.gender == "male" else "Down")
                yield
            elif task_is_active("Task_NewGameBirchSpeech_ProcessNameYesNoMenu"):
                context.emulator.press_button("Up")
                yield
            context.emulator.press_button("A")
            yield
        elif goal is StoryGoal.NAME:
            yield from self._bounded(type_in_naming_screen(self.journey.player_name, max_length=7))
        elif goal is StoryGoal.CLOCK_UI:
            if task_is_active("Task_SetClock_HandleConfirmInput"):
                context.emulator.press_button("Up")
                yield
            context.emulator.press_button("A")
            yield
        elif goal is StoryGoal.DIALOGUE:
            context.emulator.press_button(self._dialogue_button(facts))
            yield
        elif goal is StoryGoal.TRUCK:
            yield from self._bounded(navigate_to(MapRSE.INSIDE_OF_TRUCK, (4, 2), run=False, expecting_script=True))
        elif goal is StoryGoal.CLOCK:
            bedroom = MapRSE.LITTLEROOT_TOWN_BRENDANS_HOUSE_2F if male else MapRSE.LITTLEROOT_TOWN_MAYS_HOUSE_2F
            yield from self._interact(Destination(bedroom.value, (5 if male else 3, 2)), "Up")
        elif goal is StoryGoal.MOVING_IN:
            house = MapRSE.LITTLEROOT_TOWN_BRENDANS_HOUSE_1F if male else MapRSE.LITTLEROOT_TOWN_MAYS_HOUSE_1F
            yield from self._travel(Destination(house.value, (7 if male else 3, 5)))
        elif goal is StoryGoal.MEET_RIVAL:
            bedroom = MapRSE.LITTLEROOT_TOWN_MAYS_HOUSE_2F if male else MapRSE.LITTLEROOT_TOWN_BRENDANS_HOUSE_2F
            yield from self._interact(Destination(bedroom.value, (5 if male else 3, 5)), "Up")
        elif goal is StoryGoal.RESCUE:
            yield from self._travel(Destination(MapRSE.ROUTE101.value, (10, 19)))
        elif goal is StoryGoal.STARTER:
            bag = Destination(MapRSE.ROUTE101.value, (7, 15))
            if get_player_location() != (bag.map, bag.coordinates):
                yield from self._travel(bag)
            else:
                yield from ensure_facing_direction("Up")
                if not self._starter_checkpoint_is_valid():
                    yield from self._bounded(save_the_game())
                    if not self._starter_checkpoint_is_valid():
                        raise BotModeError("Saving at Birch's bag did not produce a verified pre-starter checkpoint.")
                context.emulator.press_button("A")
                yield
        elif goal is StoryGoal.STARTER_BATTLE:
            yield from self._bounded(self._evaluate_starter())
        elif goal is StoryGoal.SAVE_STARTER:
            identity = get_party().non_eggs[0].data[:8]
            yield from self._bounded(save_the_game())
            saved = get_save_data()
            if saved is None or not any(p.data[:8] == identity for p in saved.get_party()):
                raise BotModeError("The accepted shiny starter could not be verified in the cartridge save.")
            context.emulator.create_save_state(suffix="Journey_StarterCartridgeSaved")
        elif goal is StoryGoal.CHOOSE_STARTER:
            yield from self._bounded(self._starter_choice())
        elif goal is StoryGoal.HEAL:
            outside = Destination(MapRSE.OLDALE_TOWN.value, (6, 17))
            if get_player_location() != (outside.map, outside.coordinates):
                yield from self._travel(outside)
            else:
                yield from self._bounded(heal_in_pokemon_center(PokemonCenter.OldaleTown))
        elif goal is StoryGoal.TRAIN:
            spot = self._grass(MapRSE.ROUTE101)
            if get_player_location() != (spot.map, spot.coordinates):
                yield from self._travel(spot)
            else:
                self._training = PokecenterLoopController()
                self._training.battle_strategy = AgentTrainerStrategy
                self._training.verify_on_start()
                try:
                    yield from self._bounded(
                        self._training.run(
                            stop_condition=lambda: get_party().non_eggs[0].level >= self.journey.rival_level
                        )
                    )
                finally:
                    self._training = None
        elif goal is StoryGoal.RIVAL:
            yield from self._interact(Destination(MapRSE.ROUTE103.value, (10, 4)), npc=2)
        elif goal is StoryGoal.POKEDEX:
            yield from self._interact(Destination(MapRSE.LITTLEROOT_TOWN_PROFESSOR_BIRCHS_LAB.value, (6, 5)), npc=2)
        elif goal is StoryGoal.SHOES:
            yield from self._travel(Destination(MapRSE.LITTLEROOT_TOWN.value, (10, 2)))
        else:
            yield

    def _run_agent(self) -> Generator:
        if not self.is_selectable():
            raise BotModeError("Emerald Journey Agent currently requires an English retail Pokémon Emerald ROM.")
        try:
            self.journey = JourneySettings.load(context.profile.path / "emerald_journey.json")
            self.settings = AgentSettings.load(context.profile.path / "shiny_team_agent.json")
        except ValueError as error:
            raise BotModeError(str(error)) from error
        self.story_policy = EmeraldStoryPolicy(self.journey)
        previous_facts = None
        stagnant_frames = 0
        while context.bot_mode == self.name():
            if self._capture_failure:
                raise BotModeError(self._capture_failure)
            facts = self._story_facts()
            decision = self.story_policy.decide(facts)
            self._report_story(decision, facts)
            if decision.goal is StoryGoal.PAUSE:
                raise BotModeError(decision.reason)
            if decision.goal is StoryGoal.TEAM:
                if facts.map in {address.value for address in EARLY_MAPS if address.value[0] != 0}:
                    yield from self._travel(Destination(MapRSE.OLDALE_TOWN.value, (6, 17)))
                    yield
                    continue
                settings = self.settings
                if settings.shop is None:
                    settings = replace(
                        settings,
                        shop=Shop(
                            Destination(MapRSE.OLDALE_TOWN_MART.value, (3, 3)),
                            "Left",
                            entrance=Destination(MapRSE.OLDALE_TOWN.value, (14, 6)),
                        ),
                    )
                self._team_agent = ShinyTeamAgentMode(settings, active_mode_name=self.name())
                self._team_agent._known_spots.update(
                    self._grass(address) for address in (MapRSE.ROUTE101, MapRSE.ROUTE102, MapRSE.ROUTE103)
                )
                yield from self._team_agent.run()
                return
            stagnant_frames = stagnant_frames + 1 if facts == previous_facts else 0
            if stagnant_frames >= self.settings.action_timeout:
                raise BotModeError("No story progress detected within the configured limit. Inspect the reported goal.")
            previous_facts = facts
            yield from self._execute_story(decision.goal, facts)
            # A route can already be at its goal and yield zero frames. Always
            # let the game process pending coordinate events before observing again.
            yield
