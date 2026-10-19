"""Emulator adapter for the reactive shiny team policy."""

import json
from datetime import datetime, timezone
from typing import Generator

from modules.battle_state import BattleOutcome, EncounterType
from modules.battle_strategies import DefaultBattleStrategy, TurnAction
from modules.battle_strategies.catch import CatchStrategy
from modules.battle_strategies.run_away import RunAwayStrategy
from modules.context import context
from modules.encounter import EncounterInfo, handle_encounter
from modules.items import ItemPocket, get_item_bag, get_item_by_name
from modules.map import (
    get_map_all_tiles,
    get_map_data,
    get_map_data_for_current_position,
    get_effective_encounter_rates_for_current_map,
    get_wild_encounters_for_map,
)
from modules.map_data import MapFRLG, PokemonCenter, is_safari_map
from modules.map_path import PathFindingError, calculate_path
from modules.memory import GameState, get_game_state
from modules.player import get_player, get_player_location, player_avatar_is_controllable
from modules.pokemon import StatusCondition, get_species_by_name, pokemon_has_usable_damaging_move
from modules.pokemon_party import get_party
from modules.pokemon_storage import get_pokemon_storage
from modules.shiny_team_agent import (
    Action,
    AgentSettings,
    Decision,
    Destination,
    HuntOption,
    Member,
    Observation,
    ShinyTeamPolicy,
    write_status,
)
from modules.tasks import task_is_active
from ._interface import BattleAction, BotMode, BotModeError
from .util import (
    buy_in_shop,
    ensure_facing_direction,
    find_closest_pokemon_center,
    heal_in_pokemon_center,
    navigate_to,
    spin,
    wait_for_no_script_to_run,
    wait_for_player_avatar_to_be_controllable,
)
from .util.pc_interaction import PCAction, interact_with_pc


class AgentTrainerStrategy(DefaultBattleStrategy):
    # Keep the team specification and existing moves intact in this first version.
    def which_move_should_be_replaced(self, pokemon, new_move) -> int:
        return 4

    def should_allow_evolution(self, pokemon, party_index) -> bool:
        return False

    def should_flee_after_faint(self, battle_state) -> bool:
        return False

    def _handle_lead_cannot_battle(self, battle_state, util, reason=""):
        if util.get_potential_rotation_targets(battle_state) and util.can_switch():
            return TurnAction.rotate_lead(util.select_rotation_target(battle_state))
        move = util.get_strongest_move_against(
            battle_state.own_side.active_battler, battle_state.opponent.active_battler
        )
        if move is not None:
            return TurnAction.use_move(move)
        raise BotModeError("No usable move or switch is available in this trainer battle.")


class AgentRunAwayStrategy(RunAwayStrategy):
    def which_move_should_be_replaced(self, pokemon, new_move) -> int:
        return 4

    def should_allow_evolution(self, pokemon, party_index) -> bool:
        return False


class ShinyTeamAgentMode(BotMode):
    @staticmethod
    def name() -> str:
        return "Shiny Team Agent"

    @staticmethod
    def is_selectable() -> bool:
        return (
            get_game_state() is GameState.OVERWORLD
            and player_avatar_is_controllable()
            and context.rom.is_gen3
            and not context.rom.game_name.startswith("Unsupported ")
        )

    def __init__(self, settings_override: AgentSettings | None = None, active_mode_name: str | None = None):
        super().__init__()
        self.settings = AgentSettings()
        self.policy = ShinyTeamPolicy(self.settings)
        self._known_spots: set[Destination] = set()
        self._visited_maps = set()
        self._blocked_spots = set()
        self._center: PokemonCenter | None = None
        self._pending_capture: tuple[int, str] | None = None
        self._verify_capture: tuple[int, str] | None = None
        self._capture_failure: str | None = None
        self._whiteout = False
        self._last_report = None
        self._frames_without_encounter = 0
        self._last_observation = None
        self._settings_override = settings_override
        self._active_mode_name = active_mode_name or self.name()

    def _event(self, kind: str, **details) -> None:
        with (context.profile.path / "shiny_team_agent.events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "event": kind, **details}) + "\n")

    def _report(self, decision: Decision, observation: Observation) -> None:
        roster = self.policy.roster(observation.members)
        context.message = decision.reason
        signature = (decision, roster)
        if signature != self._last_report:
            write_status(
                context.profile.path / "shiny_team_agent.status.json", decision, roster, self.settings.team_size
            )
            self._event("decision", action=decision.action.value, reason=decision.reason)
            self._last_report = signature

    def _bounded(self, action: Generator) -> Generator:
        # Count only frames in which this controller runs. Battle listeners may
        # suspend it for arbitrarily long hunts/captures without timing it out.
        try:
            for _ in range(self.settings.action_timeout):
                if self._capture_failure:
                    raise BotModeError(self._capture_failure)
                if self._whiteout:
                    return
                if self._verify_capture is not None:
                    self._verify_caught_pokemon()
                    return  # A verified shiny changes the goal; discard the old action.
                try:
                    next(action)
                except StopIteration:
                    return
                yield
            raise BotModeError("Agent action timed out. Inspect the current state before resuming.")
        finally:
            action.close()

    @staticmethod
    def _pokemon():
        party = list(get_party())
        boxed = [slot.pokemon for box in get_pokemon_storage().boxes for slot in box.slots]
        return party, boxed

    def _members(self) -> tuple[Member, ...]:
        party, boxed = self._pokemon()
        return tuple(
            Member(
                identity=pokemon.data[:8].hex(),
                species=pokemon.species.name,
                shiny=pokemon.is_shiny,
                in_party=in_party,
                egg=pokemon.is_egg,
                types=tuple(t.name for t in pokemon.species.types),
            )
            for in_party, group in ((True, party), (False, boxed))
            for pokemon in group
            if not pokemon.is_empty and pokemon.is_valid
        )

    def _discover_spots(self) -> None:
        location = get_map_data_for_current_position()
        address = location.map_group_and_number
        if address in self._visited_maps:
            return
        self._visited_maps.add(address)
        x, y = location.local_position
        tiles = sorted(
            get_map_all_tiles(location), key=lambda t: abs(t.local_position[0] - x) + abs(t.local_position[1] - y)
        )
        # Keep a bounded set of nearby alternatives; refresh path feasibility on
        # every decision rather than assuming that NPCs/flags stayed unchanged.
        for tile in [t for t in tiles if t.has_encounters and not t.collision][:16]:
            self._known_spots.add(Destination(address, tile.local_position))

    def _hunts(self) -> tuple[HuntOption, ...]:
        self._discover_spots()
        current = get_map_data_for_current_position()
        effective = get_effective_encounter_rates_for_current_map()
        options = []
        for destination in sorted(self._known_spots, key=lambda s: (s.map, s.coordinates)):
            if destination in self._blocked_spots:
                continue
            tile = get_map_data(destination.map, destination.coordinates)
            # Initial scope: land encounters only. No implicit surfing, fishing,
            # cave/door traversal or story prerequisites during a hunt.
            if not tile.has_encounters or tile.is_surfable or tile.collision:
                continue
            try:
                path = calculate_path(
                    get_player_location(), (destination.map, destination.coordinates), no_surfing=True
                )
            except PathFindingError:
                continue
            if destination.map == current.map_group_and_number:
                encounters = effective.land_encounters if effective is not None else []
            else:
                raw = get_wild_encounters_for_map(*destination.map)
                encounters = raw.land_encounters if raw is not None else []
            total = sum(e.encounter_rate for e in encounters)
            if total:
                options.append(
                    HuntOption(
                        destination,
                        tuple(
                            (e.species.name, tuple(t.name for t in e.species.types), e.encounter_rate / total)
                            for e in encounters
                        ),
                        len(path),
                    )
                )
        return tuple(options)

    def _observe(self) -> Observation:
        location = get_map_data_for_current_position()
        try:
            self._center = find_closest_pokemon_center(location)
        except BotModeError:
            self._center = None
        party = get_party().non_eggs
        needs_healing = bool(party) and (
            any(
                p.current_hp / max(1, p.stats.hp) < self.settings.heal_hp_fraction
                or p.status_condition is not StatusCondition.Healthy
                for p in party
            )
            or not any(pokemon_has_usable_damaging_move(p) for p in party if p.current_hp > 0)
            or (
                any(m is not None and m.move.base_power > 0 for m in party[0].moves)
                and not pokemon_has_usable_damaging_move(party[0])
            )
        )
        shop = self.settings.shop
        can_restock = False
        if shop is not None:
            item = get_item_by_name(shop.ball)
            deficit = max(0, self.settings.min_balls - get_item_bag().number_of_balls_except_master_ball)
            try:
                self._shop_route()
                can_restock = (
                    item.price > 0
                    and get_player().money >= item.price * deficit
                    and get_item_bag().has_space_for(item, deficit)
                )
            except PathFindingError:
                pass
        return Observation(
            members=self._members(),
            balls=get_item_bag().number_of_balls_except_master_ball,
            can_receive=len(get_party()) < 6 or get_pokemon_storage().pokemon_count < 420,
            needs_healing=needs_healing,
            can_heal=self._center is not None,
            can_assemble=self._center is not None
            and self._center is not PokemonCenter.PalletTown
            and (len(get_party()) < 6 or get_pokemon_storage().pokemon_count < 420),
            can_restock=can_restock,
            hunts=self._hunts(),
            has_party=bool(party),
        )

    def on_battle_started(self, encounter: EncounterInfo | None):
        self._frames_without_encounter = 0
        if encounter is None:
            return AgentTrainerStrategy()
        if encounter.type is EncounterType.Gift:
            raise BotModeError("Start the agent after choosing a starter and receiving Poké Balls.")
        if not encounter.is_shiny:
            encounter.battle_action = BattleAction.RunAway
            return AgentRunAwayStrategy()

        # Catch every shiny, including duplicates and species outside the desired
        # roster. Such a catch does not automatically satisfy another team slot.
        context.emulator.create_save_state(suffix="Agent_ShinyEncounter")
        if get_item_bag().number_of_balls_except_master_ball == 0:
            raise BotModeError("Shiny preserved in a save state: no usable Poké Balls remain.")
        if len(get_party()) == 6 and get_pokemon_storage().pokemon_count >= 420:
            raise BotModeError("Shiny preserved in a save state: party and PC are full.")
        handle_encounter(encounter, do_not_switch_to_manual=True)
        encounter.battle_action = BattleAction.Catch
        self._pending_capture = (encounter.pokemon.personality_value, encounter.pokemon.species.name)
        self._event("shiny_encounter", species=self._pending_capture[1], personality=self._pending_capture[0])
        return CatchStrategy()

    def on_battle_ended(self, outcome: BattleOutcome) -> None:
        if self._pending_capture is not None:
            if outcome is BattleOutcome.Caught:
                self._verify_capture = self._pending_capture
            else:
                self._capture_failure = f"Shiny capture failed ({outcome.name}). The encounter save state is available."
            self._pending_capture = None

    def on_pickup_threshold_reached(self) -> bool:
        return False

    def on_whiteout(self) -> bool:
        self._whiteout = True
        self._event("whiteout")
        return True

    def on_egg_hatched(self, encounter, party_index):
        return True

    def _verify_caught_pokemon(self) -> None:
        personality, species = self._verify_capture
        party, boxed = self._pokemon()
        if not any(
            p.is_valid
            and not p.is_egg
            and p.is_shiny
            and p.personality_value == personality
            and p.species.name == species
            for p in party + boxed
        ):
            raise BotModeError(
                "The game reported a catch, but the shiny is not in party/PC. Progress has not advanced."
            )
        context.emulator.create_save_state(suffix="Agent_ShinyCaught")
        self._event("verified_capture", species=species, personality=personality)
        self._verify_capture = None
        self._blocked_spots.clear()

    def _assemble(self, observation: Observation) -> Generator:
        desired = {p.identity for p in self.policy.roster(observation.members)}
        yield from heal_in_pokemon_center(self._center)
        # Heal helper ends outside the entrance. Enter it again to use the PC.
        yield from navigate_to(*self._center.value)
        location = get_map_data_for_current_position()
        pc_positions = []
        for tile in get_map_all_tiles(location):
            if tile.tile_type != "PC":
                continue
            x, y = tile.local_position
            for coordinates, facing in (
                ((x, y + 1), "Up"),
                ((x - 1, y), "Right"),
                ((x, y - 1), "Down"),
                ((x + 1, y), "Left"),
            ):
                if not (0 <= coordinates[0] < location.map_size[0] and 0 <= coordinates[1] < location.map_size[1]):
                    continue
                try:
                    path = calculate_path(get_player_location(), (location.map_group_and_number, coordinates))
                    pc_positions.append((len(path), coordinates, facing))
                except PathFindingError:
                    pass
        if not pc_positions:
            raise BotModeError("No reachable PC found inside this Pokémon Center.")
        _, coordinates, facing = min(pc_positions)
        yield from navigate_to(location.map_group_and_number, coordinates)
        yield from ensure_facing_direction(facing)
        for _ in range(6):
            party, boxed = self._pokemon()
            wanted = next((p for p in boxed if p.data[:8].hex() in desired), None)
            if wanted is None:
                break
            actions = []
            if len(party) == 6:
                replacement = next((p for p in reversed(party) if p.data[:8].hex() not in desired), None)
                if replacement is None:
                    raise BotModeError("No party slot can be safely replaced.")
                actions.append(PCAction.deposit_pokemon_to_box(replacement))
            actions.append(PCAction.withdraw_pokemon_from_box(wanted))
            yield from interact_with_pc(actions)
            if not any(p.data[:8] == wanted.data[:8] for p in get_party()):
                raise BotModeError("PC transfer could not be verified. Inspect the party before continuing.")
        yield from navigate_to(location.map_group_and_number, (7, 8))
        context.emulator.create_save_state(suffix="Agent_TeamUpdated")

    def _shop_route(self) -> list[Destination]:
        shop = self.settings.shop
        try:
            calculate_path(get_player_location(), (shop.destination.map, shop.destination.coordinates), no_surfing=True)
            return [shop.destination]
        except PathFindingError:
            if shop.entrance is None:
                raise
        path = calculate_path(get_player_location(), (shop.entrance.map, shop.entrance.coordinates), no_surfing=True)
        if not path or not path[-1].is_warp:
            raise PathFindingError("The configured shop entrance is not a reachable door warp.")
        calculate_path(
            (path[-1].map, path[-1].coordinates), (shop.destination.map, shop.destination.coordinates), no_surfing=True
        )
        return [shop.entrance, shop.destination]

    def _restock(self) -> Generator:
        shop = self.settings.shop
        for destination in self._shop_route():
            yield from navigate_to(destination.map, destination.coordinates)
        yield from ensure_facing_direction(shop.facing)
        task = "Task_DoBuySellMenu" if context.rom.is_rs else "Task_ShopMenu"
        while not task_is_active(task):
            context.emulator.press_button("A")
            yield
        item = get_item_by_name(shop.ball)
        before = get_item_bag().number_of_balls_except_master_ball
        quantity = min(99, shop.target_balls - before, get_player().money // item.price)
        while quantity > 0 and not get_item_bag().has_space_for(item, quantity):
            quantity -= 1
        if quantity <= 0:
            raise BotModeError("Cannot buy more balls with the current bag space and money.")
        yield from buy_in_shop([(item, quantity)])
        while task_is_active(task):
            context.emulator.press_button("B")
            yield
        yield from wait_for_no_script_to_run("B")
        yield from wait_for_player_avatar_to_be_controllable()
        if get_item_bag().number_of_balls_except_master_ball <= before:
            raise BotModeError("Restocking did not increase the ball reserve.")
        if shop.entrance is not None:
            location = get_map_data_for_current_position()
            exits = []
            for warp in location.warps:
                if (warp.destination_map_group, warp.destination_map_number) != shop.entrance.map:
                    continue
                try:
                    path = calculate_path(
                        get_player_location(), (location.map_group_and_number, warp.local_coordinates)
                    )
                    exits.append((len(path), warp.local_coordinates))
                except PathFindingError:
                    pass
            if not exits:
                raise BotModeError("No reachable shop exit returns to the configured entrance map.")
            yield from navigate_to(location.map_group_and_number, min(exits)[1])

    def _hunt(self, destination: Destination) -> Generator:
        if get_player_location() != (destination.map, destination.coordinates):
            try:
                yield from self._bounded(navigate_to(destination.map, destination.coordinates))
            except BotModeError:
                self._blocked_spots.add(destination)
                self._event("unreachable_spot", map=destination.map, coordinates=destination.coordinates)
            return  # Always observe again after moving, before generating encounters.
        activity = spin()
        try:
            for _ in range(self.settings.decision_interval):
                if self._verify_capture is not None or self._capture_failure or self._whiteout:
                    return
                next(activity)
                self._frames_without_encounter += 1
                if self._frames_without_encounter >= self.settings.encounter_timeout:
                    raise BotModeError(
                        "No encounters occurred within the configured limit. Check repel, tile and party."
                    )
                yield
        finally:
            activity.close()

    def run(self) -> Generator:
        try:
            yield from self._run_agent()
        except BotModeError as error:
            decision = Decision(Action.PAUSE, str(error))
            if self._last_observation is not None:
                self._report(decision, self._last_observation)
            else:
                write_status(
                    context.profile.path / "shiny_team_agent.status.json", decision, (), self.settings.team_size
                )
            self._event("paused", reason=str(error))
            raise

    def _run_agent(self) -> Generator:
        if not self.is_selectable() or is_safari_map():
            raise BotModeError(
                "Start Shiny Team Agent in the overworld of a supported retail Gen3 ROM, outside Safari."
            )
        try:
            self.settings = self._settings_override or AgentSettings.load(
                context.profile.path / "shiny_team_agent.json"
            )
            for species in self.settings.target_species:
                get_species_by_name(species)
            if self.settings.shop is not None:
                item = get_item_by_name(self.settings.shop.ball)
                if (
                    item.pocket is not ItemPocket.PokeBalls
                    or item.name in ("Master Ball", "Safari Ball")
                    or item.price <= 0
                ):
                    raise ValueError("shop.ball must be a purchasable, usable Poké Ball.")
        except (ValueError, KeyError) as error:
            raise BotModeError(str(error)) from error
        self.policy = ShinyTeamPolicy(self.settings)
        self._known_spots.update(self.settings.hunt_spots)
        context.emulator.create_save_state(suffix="Agent_Started")
        idle_frames = 0
        previous_maintenance = None
        while context.bot_mode == self._active_mode_name:
            if get_game_state() is not GameState.OVERWORLD or not player_avatar_is_controllable():
                idle_frames += 1
                if idle_frames >= self.settings.action_timeout:
                    raise BotModeError(
                        "The agent cannot regain control of the player. Inspect the current dialog or menu."
                    )
                yield
                continue
            idle_frames = 0
            if self._capture_failure:
                raise BotModeError(self._capture_failure)
            if self._verify_capture is not None:
                self._verify_caught_pokemon()
            if self._whiteout:
                self._whiteout = False
                map, _ = get_player_location()
                door = (4, 8) if map == MapFRLG.PALLET_TOWN_PLAYERS_HOUSE_1F else (7, 8)
                yield from self._bounded(navigate_to(map, door))
                continue
            observation = self._observe()
            self._last_observation = observation
            decision = self.policy.decide(observation)
            self._report(decision, observation)
            if decision.action in (Action.HEAL, Action.ASSEMBLE, Action.RESTOCK):
                maintenance = (decision.action, observation, get_player_location())
                if maintenance == previous_maintenance:
                    raise BotModeError(
                        f"Action {decision.action.value} made no progress. Inspect the game before resuming."
                    )
                previous_maintenance = maintenance
            else:
                previous_maintenance = None
            if decision.action is Action.COMPLETE:
                context.emulator.create_save_state(suffix="Agent_ShinyTeamReady")
                context.set_manual_mode()
                return
            if decision.action is Action.PAUSE:
                raise BotModeError(decision.reason)
            if decision.action is Action.HEAL:
                yield from self._bounded(heal_in_pokemon_center(self._center))
            elif decision.action is Action.ASSEMBLE:
                yield from self._bounded(self._assemble(observation))
            elif decision.action is Action.RESTOCK:
                yield from self._bounded(self._restock())
            elif decision.action is Action.HUNT:
                yield from self._hunt(decision.destination)
