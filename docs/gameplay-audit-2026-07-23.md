# Social Empires gameplay audit

Date: 2026-07-23

This audit covers the current merged server and the rebuilt
`SocialEmpires0926bsec.swf`. It distinguishes bugs from original gameplay
rules so that a behavior is not changed merely because it is inconvenient.

## Result

The merge is consistent and the tested gameplay state is stable. The complete
automated suite passes: **108 tests, 0 failures**. A real Ruffle browser startup
also completed the loader, game configuration, player-state, village-asset and
command-persistence flows.

## Fixed and verified

### Game startup

- The page no longer assumes that the server is always on port 5050. Flash
  assets and API calls use the origin that served the page.
- Ruffle now receives Flash variables as named values rather than an indented
  query-string fragment.
- Ruffle autoplay now uses its supported `"on"` mode. The old Boolean value
  could load `SELoader.swf` without running it, which caused a blank/error
  screen and no request for the game SWF.
- The legacy loader remains the root movie, which is required because the main
  game reads its configuration from `Stage.loaderInfo.parameters`.

### Resource harvesting and production

- Harvested natural trees, stone deposits and gold deposits are removed
  permanently. They no longer install a regeneration timer.
- Reloading an established town no longer creates another random batch of
  trees, stone or gold. Initial map resources are generated once per town.
- Mine and mill state survives reloads and moving the building: workers, the
  selected production duration, start timestamp and progress are preserved.
- Mine/mill output now follows the client formula. The first worker gives the
  base output; each additional worker adds 20% of the base output. More workers
  increase the amount, not the production speed.
- Selected production options now affect the server reward with the same
  multipliers used by the client.
- A Stone Mine does not become operational with only one role filled. It
  requires both of its configured workers: **Geologist and Miner**. The role is
  not called Archaeologist in this game data.
- Invalid market trade counters are repaired and the UI no longer displays
  `NaN`.

### Interaction and controls

- Clicking XP, gold or another actionable token consumes the click before it
  reaches the map, so a selected unit does not also move to the token.
- Clicking an enemy issues an attack objective, not a simultaneous forced move.
  A ranged unit approaches only until it is in range and then attacks.
- A later empty-ground click remains a normal move order, including during an
  attack.
- The extra move icon/order no longer appears after attack or collect actions.
- Escape now clears the current selection and returns the cursor to the normal
  inquire/select tool.
- Friend cards restore their exact resting coordinates on mouse-out, so repeated
  hovering no longer moves cards out of place.

### Goals and social features

- “Recruit Friend Market” now reports `0/1` until completed rather than
  `Completed 0/0`.
- “Open Market” uses a safe `0/1` progress value rather than `Completed /1`.
- Social goal counters tolerate missing category data instead of producing
  malformed progress.
- Round Table help can only be posted to an actual linked friend. It can no
  longer target an arbitrary/nonexistent saved player.
- Saved villages are PvP opponents, not automatic neighbours. Neighbours are
  static scenario friends or players explicitly linked as friends.
- Empty “Ask friends to help you speed up” content is therefore expected when
  the player has no linked friends; it is populated when real friends exist.
- Player and neighbour cards use each save's current empire name instead of
  always showing “Emperor”.

### Recurring events

- Daily Darts, daily prize checks and troll-camp countdowns use a live client
  clock instead of the timestamp captured when the page first loaded.
- The running game checks for a new daily event every 30 seconds, so crossing a
  day boundary no longer requires logout or refresh.
- Daily Darts permits one free throw per local day. The board, shot balloons,
  collected units and free-throw timestamp survive reloads. Paid replays cost
  20 cash. Won units are placed in Gifts/Storage.
- The daily-prize configuration contains its reward content, and a successful
  claim updates the in-memory timestamp immediately so the popup cannot repeat
  during the same session.
- Clearing a troll camp persists its inactive state and four-hour timestamp.
  Reloading cannot immediately recreate it or randomize its position. A newly
  spawned camp after the real cooldown may use a new valid location.
- The displayed troll count is clamped at zero.

### PvP, quests and units

- `attack_player` is handled. The server persists pending/completed attack
  history, wins/losses, honour, a three-attacks-per-six-hours limit and a
  four-hour same-opponent cooldown.
- The PvP continent endpoint now returns deterministic profiles from available
  saved opponents instead of a static empty/stub result.
- Defender attack history and reported unit casualties are saved for writable
  local players.
- Quest casualties are reconciled on return: killed and unrecovered troops are
  removed permanently; rescued/recovered units are added once.
- Removing a dead unit updates population because population is recalculated
  from the surviving map units on load.
- Battle item rewards and completed unit collections persist. Collection cash
  is awarded only on the first completion.
- Walls that reach zero health are removed and remain absent after reload, so
  enemies can pass through the cleared tile.
- Deployed units can be dismissed with the remove/sell confirmation flow.
  Units whose source data has a zero resale value still correctly sell for zero.
- Visiting the level-14 Arthur scenario now resolves and returns a complete
  village instead of remaining on “Loading”.

### Progression

- Level is derived consistently from total XP on load and on visited-player
  responses. This keeps the level number and XP bar in the same progression
  band.
- Supreme Dragon Temple steps are idempotent, preserve their progress and
  timestamp, and cannot charge the same contribution twice. Its inter-step wait
  remains 48 hours unless skipped through the supported cash flow.
- Dragon Nest care/breeding uses the configured six-hour step for every dragon.
  The timer does not grow for the second dragon; that is the original rule.
- Moving a producer does not reset its work. Buying a new higher-tier building
  from the store is different from upgrading an existing one.
- Mine and mill worker bonuses are now based on worker-time during the active
  cycle. Adding a second villager with one minute left grants only one minute
  of the 20% extra-worker bonus; that villager earns the full bonus in the next
  cycle if left assigned.
- The server ignores the worker count submitted by the Flash collection
  command. Worker assignment, removal and cycle timestamps are persisted,
  preventing a reload or last-minute assignment from changing completed work.

### Friends and social buildings

- The bundled `AcidCaos` sample is no longer loaded as a user, neighbour or
  PvP opponent. Static Arthur maps remain scripted scenarios only.
- Saved players are unrelated by default. The new Friends page creates or
  removes an explicit reciprocal relationship.
- Explicit friends are included in the in-game helper list. `hire_worker` now
  fills and persists one social-building role, rejects unknown players and
  prevents the same friend from filling the same building repeatedly.
- For Round Table requests, a linked local friend is treated as accepting
  immediately because the original Facebook callback no longer exists.
- Cash staffing still follows each building's configured role count and cost.
  A Stone Mine therefore needs both Geologist and Miner; a Market needs all
  three configured roles before it opens.

### Reverse-proxy loading

- Ruffle, SWF, asset and API URLs are generated from the browser-facing request
  origin. One trusted Nginx proxy hop is honored, so `X-Forwarded-Proto` and
  the forwarded host produce the correct public URLs.
- Nginx no longer needs to decompress and rewrite HTML with `sub_filter`.
  The working configuration is documented in `docs/reverse-proxy.md`.

## Expected original behavior (not bugs)

| Observation | Expected behavior |
| --- | --- |
| Fast Collect does not start distant natural gold/stone/tree work | Fast Collect collects already-ready output. Natural deposits still require a villager action. |
| A second worker is placed in a mine or mill | Output rises by up to 20% of base per extra worker, proportional to that worker's participation in the cycle; the timer does not become shorter. |
| “Select with double click” seems incomplete | Double-click one of the three spearmen to select the group, then move the group. The mission completes when a movement is sent with more than one selected troop; merely double-clicking is not the completion event. |
| Some buildings have no Upgrade button | The button exists only when that item has a valid next-tier chain. Max-tier, special and standalone buildings have no upgrade. |
| Store buildings are not strictly sorted by level | The original store uses category/curated ordering plus level locks, not a global numeric level sort. |
| Buying the next building from scratch versus upgrading | Scratch purchase pays the full listed price and creates another building. Upgrade replaces the existing building and charges the next tier's full cost minus the old building's 5% resale credit. |
| Several harvest targets are clicked for one villager | A normal villager has one active resource job. Multiple selection is not an unlimited persistent job queue; area harvesting/group queue capacity is controlled by the client rules. |
| Market is not immediately usable | It must have its configured helper/staff requirement satisfied. It then has 20 trades in a 20-hour period. |
| Early Mill/Gold Mine upgrades look cheap | Those values are in the original progression table. Later tiers rise sharply. Wood, food and gold are intentionally abundant; stone is the primary scarce construction/defence resource. |
| A training/breeding stable appears not to count down | The stable's valid-pair conversion is an immediate command flow rather than a long-running production timer. The Dragon Nest is the timed six-hour flow. |
| A blank friend-help box appears | It is correct when there are no explicitly linked friends; unrelated local saves are no longer treated as friends. |
| Antimatter Wizard resale is zero | Its source resale value is zero. Dismissal is allowed, but it does not grant cash. |

## Animals

- Cows, sheep and wild horses are generated by the client in a level-scaled
  initialization/daily batch and are persisted as normal units.
- The natural-resource reload guard does not suppress animals, but the animal
  daily stamp prevents another daily allocation from being granted simply by
  refreshing.
- In the browser smoke test, a new level-14 map created its one initial
  level-appropriate animal batch and saved it. A subsequent load used the saved
  state rather than re-awarding the same batch.

## Building damage and repair

- A partially damaged building should be repaired by assigning a villager; it
  is not purchased again from scratch.
- A destroyed building/wall is removed. Rebuilding it is a new purchase.
- The server persists destruction and battle casualties. Exact intermediate HP
  for every partially damaged ordinary building remains client-side state; a
  refresh may restore an undestroyed building's configured full HP. This is a
  remaining fidelity limitation, not treated as completed persistence.

## Remaining limitations

- PvP now has real opponent lists, histories, cooldowns and casualties, but it
  is still local asynchronous PvP. It does not simulate an online defender or
  persist every intermediate damaged-building HP value into the opponent save.
- Static scenario villages are read-only. Attacks against them record the
  attacker's outcome but do not rewrite the scenario file.
- Villager pathfinding can still be obstructed by a particular player-built
  layout. The persistence fixes prevent a valid active mine/mill job from being
  lost on refresh or building movement, but they do not redesign Flash
  pathfinding.

## Verification

- Automated: 112 passed, 0 failed across authentication, commands, economy,
  gameplay state, persistence, routes, SWF patch integrity and tournaments.
- Browser: Ruffle loaded `SELoader.swf`, the rebuilt game SWF, game
  configuration, player state, fonts, village sprites and combat assets; it
  then posted and persisted initialization commands without CORS errors.
- SWF integrity checks confirm that the earlier action-click, attack movement,
  resource-respawn, unit-removal and UI fixes are still present after the
  rebuild.
