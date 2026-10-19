"""ROM-independent observations and policy for the reactive shiny team agent.

The emulator adapter supplies facts. Decisions are recalculated from those facts;
neither encounter logs nor a persisted counter are evidence of owning a Pokémon.
"""

import json
import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


@dataclass(frozen=True)
class Destination:
    map: tuple[int, int]
    coordinates: tuple[int, int]


@dataclass(frozen=True)
class Shop:
    destination: Destination
    facing: str
    ball: str = "Poké Ball"
    target_balls: int = 50
    entrance: Destination | None = None


@dataclass(frozen=True)
class AgentSettings:
    team_size: int = 6
    unique_species: bool = True
    target_species: tuple[str, ...] = ()
    min_balls: int = 10
    heal_hp_fraction: float = 0.45
    decision_interval: int = 120
    action_timeout: int = 18000
    encounter_timeout: int = 36000
    hunt_spots: tuple[Destination, ...] = ()
    shop: Shop | None = None

    @classmethod
    def load(cls, path: Path) -> "AgentSettings":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object.")
            unknown = data.keys() - cls.__dataclass_fields__.keys()
            if unknown:
                raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
            for name, minimum, maximum in (
                ("team_size", 1, 6),
                ("min_balls", 1, 99),
                ("decision_interval", 1, 600),
                ("action_timeout", 60, 216000),
                ("encounter_timeout", 60, 2160000),
            ):
                value = data.get(name, getattr(cls(), name))
                if type(value) is not int or not minimum <= value <= maximum:
                    raise ValueError(f"{name} must be an integer between {minimum} and {maximum}.")
            if type(data.get("unique_species", True)) is not bool:
                raise ValueError("unique_species must be true or false.")
            hp = data.get("heal_hp_fraction", 0.45)
            if type(hp) not in (int, float) or not math.isfinite(hp) or not 0 < hp <= 1:
                raise ValueError("heal_hp_fraction must be greater than 0 and at most 1.")
            species = data.get("target_species", [])
            if not isinstance(species, list) or any(not isinstance(s, str) or not s.strip() for s in species):
                raise ValueError("target_species must be a list of non-empty species names.")
            if len(set(species)) != len(species):
                raise ValueError("target_species contains duplicates.")
            data["target_species"] = tuple(species)
            if data.get("unique_species", True) and species and len(species) < data.get("team_size", 6):
                raise ValueError("List at least team_size target species, or disable unique_species.")
            spots = data.get("hunt_spots", [])
            if not isinstance(spots, list):
                raise ValueError("hunt_spots must be a list.")
            data["hunt_spots"] = tuple(_destination(s) for s in spots)
            shop = data.get("shop")
            if shop is not None:
                if not isinstance(shop, dict) or shop.keys() - {
                    "map",
                    "coordinates",
                    "facing",
                    "ball",
                    "target_balls",
                    "entrance",
                }:
                    raise ValueError("Invalid shop settings.")
                facing = shop.get("facing")
                if facing not in ("Up", "Right", "Down", "Left"):
                    raise ValueError("shop.facing must be Up, Right, Down or Left.")
                amount = shop.get("target_balls", 50)
                if type(amount) is not int or not data.get("min_balls", 10) <= amount <= 99:
                    raise ValueError("shop.target_balls must be between min_balls and 99.")
                ball = shop.get("ball", "Poké Ball")
                if not isinstance(ball, str) or not ball:
                    raise ValueError("shop.ball must be an item name.")
                entrance = _destination(shop["entrance"]) if shop.get("entrance") is not None else None
                data["shop"] = Shop(_destination(shop, shop=True), facing, ball, amount, entrance)
            return cls(**data)
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError(f"Invalid {path.name}: {error}") from error


def _destination(data: dict, shop: bool = False) -> Destination:
    if not isinstance(data, dict) or (not shop and data.keys() != {"map", "coordinates"}):
        raise ValueError("A hunt spot needs exactly map and coordinates.")
    pairs = []
    for name in ("map", "coordinates"):
        pair = data[name]
        if not isinstance(pair, list) or len(pair) != 2 or any(type(v) is not int or v < 0 for v in pair):
            raise ValueError(f"{name} must contain two non-negative integers.")
        pairs.append(tuple(pair))
    return Destination(*pairs)


@dataclass(frozen=True)
class Member:
    identity: str
    species: str
    shiny: bool
    in_party: bool
    egg: bool = False
    types: tuple[str, ...] = ()


@dataclass(frozen=True)
class HuntOption:
    destination: Destination
    # Probability mass per species, after applying current repel/ability effects.
    encounters: tuple[tuple[str, tuple[str, ...], float], ...]
    travel_steps: int = 0


@dataclass(frozen=True)
class Observation:
    members: tuple[Member, ...]
    balls: int
    can_receive: bool
    needs_healing: bool
    can_heal: bool
    can_assemble: bool
    can_restock: bool
    hunts: tuple[HuntOption, ...] = ()
    has_party: bool = True


class Action(str, Enum):
    HUNT = "hunt"
    HEAL = "heal"
    RESTOCK = "restock"
    ASSEMBLE = "assemble"
    COMPLETE = "complete"
    PAUSE = "pause"


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    destination: Destination | None = None
    score: float = 0


class ShinyTeamPolicy:
    """Safety constraints followed by utility scoring of feasible hunt locations."""

    def __init__(self, settings: AgentSettings):
        self.settings = settings

    def roster(self, members: tuple[Member, ...]) -> tuple[Member, ...]:
        selected = []
        identities = set()
        species = set()
        # Prefer existing party members to avoid unnecessary PC swaps.
        for member in sorted(members, key=lambda p: not p.in_party):
            if not member.shiny or member.egg or member.identity in identities:
                continue
            if self.settings.target_species and member.species not in self.settings.target_species:
                continue
            if self.settings.unique_species and member.species in species:
                continue
            identities.add(member.identity)
            species.add(member.species)
            selected.append(member)
            if len(selected) == self.settings.team_size:
                break
        return tuple(selected)

    def decide(self, state: Observation) -> Decision:
        roster = self.roster(state.members)
        count = len(roster)
        if count >= self.settings.team_size and all(p.in_party for p in roster):
            return Decision(Action.COMPLETE, f"Shiny team ready: {count}/{self.settings.team_size} in the party.")
        if not state.has_party:
            return Decision(Action.PAUSE, "No usable party. Start from an overworld save with a Pokémon.")
        if state.needs_healing:
            if state.can_heal:
                return Decision(Action.HEAL, "The party needs HP, status or move PP restored before continuing.")
            return Decision(Action.PAUSE, "The party needs healing, but no reachable Pokémon Center is known.")
        if any(not p.in_party for p in roster):
            if state.can_assemble:
                return Decision(Action.ASSEMBLE, "A shiny team member is in the PC; bring it into the party.")
            return Decision(Action.PAUSE, "A shiny team member is boxed, but no usable Pokémon Center PC is reachable.")
        if not state.can_receive:
            return Decision(Action.PAUSE, "Party and PC are full. Make room before hunting another shiny.")
        if state.balls < self.settings.min_balls:
            if state.can_restock:
                return Decision(Action.RESTOCK, f"Only {state.balls} usable balls remain; replenish the reserve.")
            return Decision(Action.PAUSE, "Not enough usable Poké Balls. Refill them or configure a reachable shop.")

        owned_species = {p.species for p in roster}
        owned_types = {t for p in roster for t in p.types}
        options = []
        for hunt in state.hunts:
            eligible = [
                (species, types, probability)
                for species, types, probability in hunt.encounters
                if (not self.settings.target_species or species in self.settings.target_species)
                and (not self.settings.unique_species or species not in owned_species)
            ]
            probability = sum(p for _, _, p in eligible)
            if probability <= 0:
                continue
            coverage = sum(p for _, types, p in eligible if set(types) - owned_types)
            score = (probability + coverage * 0.2) / (1 + hunt.travel_steps / 100)
            options.append(
                Decision(
                    Action.HUNT,
                    f"Hunt shiny {count + 1}/{self.settings.team_size}: "
                    f"{probability:.0%} of encounters can fill an open team slot.",
                    hunt.destination,
                    score,
                )
            )
        if options:
            return max(options, key=lambda option: option.score)
        return Decision(
            Action.PAUSE, "No reachable hunt spot can add a team member. Move to another area or add hunt_spots."
        )


def write_status(path: Path, decision: Decision, roster: tuple[Member, ...], team_size: int) -> None:
    """A status report only, never restored as game truth after loading a save."""
    data = {
        "action": decision.action.value,
        "reason": decision.reason,
        "team_size": team_size,
        "roster": [{"identity": p.identity, "species": p.species, "in_party": p.in_party} for p in roster],
        "destination": (
            None
            if decision.destination is None
            else {
                "map": decision.destination.map,
                "coordinates": decision.destination.coordinates,
            }
        ),
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
