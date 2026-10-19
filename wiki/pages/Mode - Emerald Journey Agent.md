# Emerald Journey Agent

This mode can start with a **new profile and no save game**, from the title/menu
or after choosing **New Game** in English retail Pokémon Emerald. It drives the
opening story, hunts a shiny starter, unlocks catching, then builds the shiny
party with the existing Shiny Team Agent.

## Start from New Game

To preselect the agent before creating the profile, run this from the repository
with the bot environment active:

```sh
python pokebot.py --bot-mode "Emerald Journey Agent"
```

The profile selector opens. Choose **New Game**, select Emerald and create the
profile; the chosen agent then starts automatically. In this checkout the prepared
environment can be used directly as `.venv/bin/python` instead of `python`.

1. Put your own English Emerald ROM into `roms/`.
2. Create a profile with **New Game** in the bot's profile screen.
3. Select **Emerald Journey Agent** in the bot mode menu. This mode is selectable
   on the title and main menu as well as in the overworld.

The third step is only needed when launching without the `--bot-mode` option.

For an existing profile, including a newly created empty one, use:

```sh
python pokebot.py PROFILE_NAME --bot-mode "Emerald Journey Agent"
```

The agent starts a new game on an empty cartridge. On an existing save it
selects **Continue**, and derives the next goal from live progress. To deliberately
start over, create a separate new profile or choose New Game yourself first.

## What the first story model knows

It can choose goals for Birch's introduction, player naming, the moving truck,
the bedroom clock, the TV scene, meeting the rival, rescuing Birch, selecting
and hunting the starter, healing/training for Route 103, the first rival battle,
returning to Birch for the Pokédex and balls, and receiving the running shoes.

These are **conditions and goals**, not a persisted list of completed steps.
For example, owning the Pokédex skips the entire startup section even if the
player loaded a later save with different intro variables. A low-health starter
interrupts the rival goal for healing. A lost rival battle resumes from current
flags and health; the rival is only marked defeated after the game sets its flag.

The observer reads maps/coordinates, active callbacks/tasks/scripts, party
state, named story variables, badges and milestone flags. The map navigator
reads connected maps and warps and chooses the next door or staircase from the
current map. It replans after map transitions and scripted events. It never
sets story flags, teleports or invents completed milestones.

The implemented story coverage ends at **catching unlocked**, followed by
assembling the requested shiny team. It does not yet plan the remaining gym,
HM, villain-team or Champion storyline. Reading badge flags is groundwork for
future story goals, not a claim of complete game coverage.

## Shiny starter and team

The default is a male trainer named **BOT** and a **shiny Mudkip**. Before the
first selection the agent creates and verifies an ordinary in-game save facing
Birch's bag. Once the starter is visible and the first battle is ready for input,
it either keeps the shiny and fights, or soft-resets a non-shiny from that verified
checkpoint. It waits for naturally different RNG states before confirming each
roll, using the bot's shared roll-history file. No shiny odds are changed.
After the Birch rescue/dialogue the accepted shiny is also saved and verified in
the cartridge save before training or further story actions.

A reset is refused if no matching pre-starter cartridge save can be verified.
An already progressed save is not rolled back to replace its original starter;
the shiny team policy can replace that team member later.

After the first rival is beaten, the agent obtains the Pokédex and balls and
unlocks the running shoes. It gives the team controller candidate grass locations
on Routes 101, 102 and 103. The controller chooses among feasible candidates
according to missing species, type coverage and distance. It also receives a
default Oldale Mart route, including the entrance and return exit. A configured
shop in `shiny_team_agent.json` takes priority over this default.

## Configuration

Optionally create `profiles/PROFILE_NAME/emerald_journey.json`:

```json
{
  "player_name": "BOT",
  "gender": "male",
  "starter": "Mudkip",
  "shiny_starter": true,
  "rival_level": 7
}
```

Both player genders and Treecko/Torchic/Mudkip are modelled. Set `shiny_starter`
to false to accept the chosen starter on its first roll. `rival_level` controls
preparation before the first rival battle. The clock's default time is confirmed.
The optional starter nickname is declined, and the request to meet the rival is
accepted. Character names use 1–7 English letters/digits.

Team size, species requirements, ball reserves and timeouts still come from
`shiny_team_agent.json`; see [Shiny Team Agent](Mode%20-%20Shiny%20Team%20Agent.md).
For a short first milestone set `team_size` to 1. With the default shiny starter,
that goal is already satisfied once catching and the running shoes are unlocked.

`emerald_journey.status.json` contains the current goal and observed story facts.
The shared `shiny_team_agent.events.jsonl` includes story-goal changes and starter
retries/acceptance. Reports are never loaded as proof of progress.

## Validation and current limits

Policy and adapter scenarios can run without a ROM:

```sh
python -m unittest tests.test_emerald_journey_policy tests.test_emerald_journey_mode
```

The opening model was checked against
[pret/pokeemerald](https://github.com/pret/pokeemerald/tree/731ad5bfd6e6f265508d0efcca0ba42f9dcf5881),
including `src/main_menu.c`, `src/starter_choose.c`, `data/scripts/players_house.inc`
and the Littleroot/Route101/Route103 map scripts. No ROM or game assets are supplied.
Those source checks and synthetic scenarios do not verify a real playthrough.
Navigation, dialogs, the clock and shiny hunting still require a live emulator
run with the user's ROM before unattended operation is confirmed.

English retail Emerald is the first story adapter. ROM hacks and other versions
need their own story/compatibility adapters. Unknown scenes or missing resources
pause with a reason instead of pretending that the story has advanced.
