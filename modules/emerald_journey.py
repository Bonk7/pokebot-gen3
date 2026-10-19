"""Story facts, goal selection and map routing, independent of the emulator.

Story facts are read afresh from the game. A report or completed action is not
proof that a milestone happened, which also allows resuming older saves.
"""

import json
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from modules.shiny_team_agent import Destination

STORY_FLAGS = (
    "SET_WALL_CLOCK",
    "RESCUED_BIRCH",
    "SYS_POKEMON_GET",
    "SYS_POKEDEX_GET",
    "DEFEATED_RIVAL_ROUTE103",
    "RECEIVED_RUNNING_SHOES",
    "SYS_GAME_CLEAR",
    *(f"BADGE{i:02}_GET" for i in range(1, 9)),
)
STORY_VARS = (
    "LITTLEROOT_INTRO_STATE",
    "LITTLEROOT_RIVAL_STATE",
    "LITTLEROOT_TOWN_STATE",
    "BIRCH_LAB_STATE",
    "ROUTE101_STATE",
)


@dataclass(frozen=True)
class JourneySettings:
    player_name: str = "BOT"
    gender: str = "male"
    starter: str = "Mudkip"
    shiny_starter: bool = True
    rival_level: int = 7

    @classmethod
    def load(cls, path: Path) -> "JourneySettings":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.keys() - cls.__dataclass_fields__.keys():
                raise ValueError("Expected a JSON object with known journey settings.")
            settings = cls(**data)
            if not isinstance(settings.player_name, str) or not 1 <= len(settings.player_name) <= 7:
                raise ValueError("player_name must contain 1 to 7 characters.")
            if any(
                c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789" for c in settings.player_name
            ):
                raise ValueError("Use letters and digits for player_name.")
            if settings.gender not in ("male", "female"):
                raise ValueError("gender must be male or female.")
            if settings.starter not in ("Treecko", "Torchic", "Mudkip"):
                raise ValueError("starter must be Treecko, Torchic or Mudkip.")
            if type(settings.shiny_starter) is not bool:
                raise ValueError("shiny_starter must be true or false.")
            if type(settings.rival_level) is not int or not 5 <= settings.rival_level <= 15:
                raise ValueError("rival_level must be between 5 and 15.")
            return settings
        except (ValueError, TypeError) as error:
            raise ValueError(f"Invalid {path.name}: {error}") from error


class StoryGoal(str, Enum):
    BOOT = "start_or_continue"
    INTRO = "birch_introduction"
    NAME = "enter_player_name"
    CLOCK_UI = "confirm_clock"
    DIALOGUE = "finish_story_dialogue"
    TRUCK = "leave_moving_truck"
    CLOCK = "set_bedroom_clock"
    MOVING_IN = "finish_moving_in"
    MEET_RIVAL = "meet_rival_at_home"
    RESCUE = "reach_birchs_bag"
    STARTER = "hunt_shiny_starter"
    CHOOSE_STARTER = "choose_starter"
    STARTER_BATTLE = "evaluate_starter"
    SAVE_STARTER = "save_accepted_shiny_starter"
    HEAL = "heal_before_story_battle"
    TRAIN = "prepare_for_rival"
    RIVAL = "defeat_route103_rival"
    POKEDEX = "receive_pokedex_and_balls"
    SHOES = "receive_running_shoes"
    TEAM = "build_shiny_team"
    WAIT = "wait_for_transition"
    PAUSE = "unsupported_situation"


@dataclass(frozen=True)
class StoryFacts:
    screen: str
    map: tuple[int, int] | None = None
    position: tuple[int, int] | None = None
    gender: str = "male"
    flags: frozenset[str] = frozenset()
    variables: tuple[tuple[str, int], ...] = ()
    scripts: tuple[str, ...] = ()
    has_starter: bool = False
    starter_level: int = 0
    starter_shiny: bool = False
    needs_healing: bool = False
    first_battle: bool = False
    starter_saved: bool = True

    def var(self, name: str) -> int:
        return dict(self.variables).get(name, 0)


@dataclass(frozen=True)
class StoryDecision:
    goal: StoryGoal
    reason: str


class EmeraldStoryPolicy:
    def __init__(self, settings: JourneySettings):
        self.settings = settings

    def decide(self, facts: StoryFacts) -> StoryDecision:
        if facts.screen == "boot":
            return StoryDecision(StoryGoal.BOOT, "Start a fresh profile, or continue its existing save.")
        if facts.screen == "intro":
            return StoryDecision(StoryGoal.INTRO, "Complete Birch's introduction and character selection.")
        if facts.screen == "naming":
            return StoryDecision(StoryGoal.NAME, "Enter the configured player name.")
        if facts.screen == "clock":
            return StoryDecision(StoryGoal.CLOCK_UI, "Confirm the bedroom clock to unlock the opening story.")
        if facts.screen == "starter_choice":
            return StoryDecision(StoryGoal.CHOOSE_STARTER, "Select the configured starter and confirm a fresh roll.")
        if facts.first_battle:
            return StoryDecision(StoryGoal.STARTER_BATTLE, "Check the visible starter before advancing the story.")
        if facts.screen in ("battle", "transition"):
            return StoryDecision(StoryGoal.WAIT, "Wait for the current battle or map transition.")
        if facts.screen == "dialogue":
            return StoryDecision(StoryGoal.DIALOGUE, "Finish the active story event before choosing the next goal.")
        if facts.screen != "overworld":
            return StoryDecision(StoryGoal.PAUSE, "This screen is not recognised by the Emerald story model.")
        if facts.map == (25, 40):
            return StoryDecision(StoryGoal.TRUCK, "Leave the moving truck to begin the Littleroot introduction.")
        # Later saves must not replay early-game prerequisites just because one
        # incidental intro variable no longer has its original value.
        if "SYS_POKEDEX_GET" in facts.flags:
            if "RECEIVED_RUNNING_SHOES" not in facts.flags:
                return StoryDecision(
                    StoryGoal.SHOES, "The Pokédex is owned; collect the running shoes on leaving town."
                )
            return StoryDecision(
                StoryGoal.TEAM, "Catching is unlocked. Work toward the shiny party from the live save."
            )
        if "SET_WALL_CLOCK" not in facts.flags:
            return StoryDecision(StoryGoal.CLOCK, "The clock is unset. Visit the player's bedroom clock.")
        if facts.var("LITTLEROOT_INTRO_STATE") < 7:
            return StoryDecision(StoryGoal.MOVING_IN, "The clock is set; finish the downstairs TV scene.")
        if facts.var("LITTLEROOT_RIVAL_STATE") < 3 and not facts.has_starter:
            return StoryDecision(StoryGoal.MEET_RIVAL, "Meet the rival at home to unlock the Birch rescue.")
        if not facts.has_starter:
            if facts.var("ROUTE101_STATE") < 2:
                return StoryDecision(StoryGoal.RESCUE, "Birch needs help on Route 101; reach the rescue scene.")
            return StoryDecision(StoryGoal.STARTER, "Choose and hunt the starter before continuing the story.")
        if facts.starter_shiny and not facts.starter_saved:
            return StoryDecision(
                StoryGoal.SAVE_STARTER, "Preserve the accepted shiny starter in an ordinary cartridge save."
            )
        if facts.needs_healing:
            return StoryDecision(StoryGoal.HEAL, "Restore the party before training or challenging the rival.")
        if "DEFEATED_RIVAL_ROUTE103" in facts.flags:
            return StoryDecision(StoryGoal.POKEDEX, "The rival is defeated. Return to Birch for the Pokédex and balls.")
        if facts.starter_level < self.settings.rival_level:
            return StoryDecision(
                StoryGoal.TRAIN, "Prepare the starter for the rival using nearby encounters and healing."
            )
        return StoryDecision(StoryGoal.RIVAL, "The starter is ready. Find the rival on Route 103.")


@dataclass(frozen=True)
class MapEdge:
    source: tuple[int, int]
    target: tuple[int, int]
    # A door/stair/exit on the source map. None denotes a connected map edge.
    warp: tuple[int, int] | None = None


def next_navigation_goal(
    source: tuple[int, int],
    destination: Destination,
    edges: tuple[MapEdge, ...],
) -> Destination:
    """Choose the next door from a live map graph; do not store a route index."""
    queue = deque([(source, ())])
    seen = {source}
    while queue:
        current, path = queue.popleft()
        if current == destination.map:
            for edge in path:
                if edge.warp is not None:
                    return Destination(edge.source, edge.warp)
            return destination
        for edge in edges:
            if edge.source == current and edge.target not in seen:
                seen.add(edge.target)
                queue.append((edge.target, (*path, edge)))
    raise ValueError("No route to the story goal exists in the known map graph.")
