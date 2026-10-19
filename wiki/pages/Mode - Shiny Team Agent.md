# Shiny Team Agent (first version)

This mode builds a shiny party with a reactive, local decision policy. It observes
the live game and chooses between hunting, healing, restocking and PC transfers.
It scores feasible hunt spots by the probability of filling a missing species,
type coverage and travel distance. A caught shiny changes the next decision.
It requires no AI service, API key or trained model. It does not learn a policy
from experience; its priorities and utility scoring are currently implemented in Python.

## Start

To begin from **New Game** in English Emerald, select
[Emerald Journey Agent](Mode%20-%20Emerald%20Journey%20Agent.md). The standalone
mode described below expects an existing overworld save.

1. Use a supported retail Ruby/Sapphire/Emerald/FireRed/LeafGreen ROM and a profile.
2. Start from a controllable overworld save, after obtaining a starter and Poké Balls.
3. Have at least 10 usable Poké Balls and a party capable of battling. A nearby
   Pokémon Center is needed for unattended healing and PC transfers.
4. Select **Shiny Team Agent**, or start an existing profile with:

   ```sh
   python pokebot.py PROFILE_NAME --bot-mode "Shiny Team Agent"
   ```

Without configuration, the goal is six shiny Pokémon of different species in
the party. Existing owned shinies count; eggs, duplicate identities, and
duplicate species do not. Shinies in the PC are transferred to the party at a
reachable Pokémon Center. Replaced party members are deposited, never released.
The mode pauses when the requested shiny party is assembled.

Start on or near a land encounter area. The agent discovers nearby hunt tiles
on visited maps and remembers them for the current session, including after
healing. If the current area has no missing species, it chooses another known,
reachable hunt spot. If none exists, it pauses with a reason.

## Optional profile configuration

Create `profiles/PROFILE_NAME/shiny_team_agent.json`:

```json
{
  "team_size": 6,
  "unique_species": true,
  "target_species": [],
  "min_balls": 10,
  "heal_hp_fraction": 0.45,
  "decision_interval": 120,
  "action_timeout": 18000,
  "encounter_timeout": 36000,
  "hunt_spots": [],
  "shop": null
}
```

Set `team_size` to 1 for the first one-shiny milestone. With `target_species`,
only those exact English species names fill team slots. List at least
`team_size` species when `unique_species` is true. All other wild shinies are
still caught and preserved, but do not fill those slots.

Additional `hunt_spots` entries have the form
`{"map": [GROUP, NUMBER], "coordinates": [X, Y]}`. These are candidate goals,
not ordered instructions. Use the debug map/player tabs to obtain coordinates.
Paths must be reachable on connected maps with current abilities. Land hunting
is supported; fishing, Surf transitions, doors and story unlocks are not planned
automatically. Remote encounter scores use base rates; current-map scores use
effective rates including repel and ability effects. The agent observes again
on arrival before hunting. It will not take scripted story routes to unlock a spot.

To enable shopping, replace `shop: null` with an object containing `map`,
`coordinates`, `facing` (`Up`, `Right`, `Down`, `Left`), `ball` (default `Poké Ball`)
and `target_balls` (default 50). The coordinates must be a reachable standing tile
next to the shop clerk, with the specified facing direction. The agent buys up
to the target reserve within available money and bag space. Without a configured
shop it pauses when the reserve is below `min_balls`.

For an indoor Mart, also set `entrance` to
`{"map": [OUTSIDE_GROUP, OUTSIDE_NUMBER], "coordinates": [DOOR_X, DOOR_Y]}`.
The door must lead to the map containing the clerk. The agent verifies the
outside-to-door and inside-to-clerk paths, enters, shops and finds the return
warp. Without `entrance`, the clerk must already be reachable on connected maps.

## Capture and recovery

Every wild shiny is protected with an encounter save state, including shinies
on the ordinary catch block list. This mode explicitly enables shiny catching
even if the ordinary `battle.yml` auto-catch option is disabled. It uses the
existing catch strategy, excludes Master Balls, and catches duplicates too.

A shiny only advances the goal after the battle reports `Caught` AND its
personality/species/shininess are verified in the actual party or PC. A second
save state preserves the verified catch. A failed catch pauses the agent and
leaves the encounter save state available for inspection; it does not manipulate
RNG, reload failed captures or manufacture shinies.

The initial game state, verified catches, PC team updates and completed team
are checkpointed through the bot's existing save-state system. These are emulator
save states, not explicit cartridge saves. The save-state system also updates
the profile's `current_state.ss1`, following normal bot behavior.

Health, status and exhausted attacking moves trigger healing when reachable.
The agent handles a whiteout by leaving the healing location and replanning.
Unexpected trainer battles use the existing battle heuristics. New move
replacement and evolution are declined in this first version so that team
specifications remain stable. Hunting has no overall encounter-count limit;
a separate watchdog pauses if no encounter occurs for `encounter_timeout`
active controller frames. Actions and idle waits also have bounded timeouts.

The bot message explains its current decision. The profile also contains:

- `shiny_team_agent.status.json`: most recent decision and observed shiny roster.
- `shiny_team_agent.events.jsonl`: decisions, shiny encounters, verified catches
  and recovery events.

These files are reports. They are never trusted as evidence of ownership after
loading or resetting the game; the roster is always recalculated from memory.

## Scope and validation

This is a reactive, goal-driven first agent. Full story playthroughs, badge/HM
planning, shiny starter resets and ROM-hack compatibility are future work.
Once the shiny goal is met, this version checkpoints and returns to Manual.

ROM-free decision scenarios can be run with:

```sh
python -m unittest tests.test_shiny_team_policy
```

Adapter tests additionally need the ordinary bot Python dependencies but no ROM:

```sh
python -m unittest tests.test_shiny_team_agent_mode
```

Real emulator play remains necessary to validate navigation, shopping and PC
interaction for a chosen game/profile. Begin with `team_size: 1` on a copied
profile and verify a heal-and-return cycle before leaving the agent unattended.
