# Self-play training data

The `search` strategy also records per-decision search statistics: sampled-world
counts, each shortlisted candidate's mean utility and sample count, and pruning
status. These are under `decision.search`; use those estimates rather than its
ordering scores as prospective value targets. See [Search AI](SEARCH_AI.md).

Run from the repository root, without starting a web server:

```bash
~/.venv/bin/python -m levelup.simulation \
  --games 100 --seed 0 --training --exploration 0.10 \
  --strategies rule_based rule_based rule_based rule_based \
  --output logs/training/selfplay-000.jsonl.gz
```

Each seed starts an independent game and plays one round. Add `--match` to play
complete matches through A. Seat order is south, east, north, west; seats 0/2 and
1/3 are partners. Any registered `AIStrategy` can be selected, including future
model checkpoints registered under distinct strategy IDs.

`--training` implies `--trace`. Files append rather than overwrite; `.gz` enables
gzip compression. Use a separate output file and disjoint seed range per worker.
Do not have multiple processes append to the same file. The Python API is:

```python
from levelup.simulation import Simulation

with Simulation(seed=42, log_path="logs/training/run.jsonl.gz",
                training=True, exploration=0.10, policy_seed=123) as sim:
    result = sim.run_round()
```

## Exploration and reproducibility

At each decision, probability `1 - exploration` selects the top-ranked candidate;
the remaining probability samples uniformly from **all supplied candidates**.
Thus candidate 0 has probability `1 - epsilon + epsilon / N`, and every other
candidate has probability `epsilon / N`. Selecting candidate 0 through the random
branch still counts as exploration. Zero exploration preserves normal AI choices.

Exploration uses a separate RNG from deck shuffling. Its seed defaults to the
game seed, or can be set with `--policy-seed`. The log records both seeds, strategy
IDs, exploration probability and a SHA-256 fingerprint of the engine, simulator,
training serializer and built-in AI sources. Custom strategies must manage and
record their own external randomness/checkpoint versions; register different
checkpoints under different IDs. Reproducibility requires the same code and
strategy implementations, not only the same deck seed.

The current candidate generator is **bounded**, not exhaustive. Every candidate
actually supplied to the ranking policy is logged, including the score and
reason breakdown. The run metadata records its ID and enumeration limits.
There is no extra policy evaluation merely to produce the training log.

## Schema version 2

Every event has `schema_version`, `run_id`, `round_id`, `round`, `deal_id`, and
`timestamp`. `run_id` identifies one seeded game/match; `round_id` identifies a
round; `decision_id` identifies one ranking query and is the grouping key for a
candidate ranker. `deal_id` distinguishes redeals of the same round.

| Event | Contents |
| --- | --- |
| `run_start` | Seeds, strategy IDs, exploration, source fingerprint, candidate-generator metadata and reward definition. |
| `decision` | Acting seat, version, full player observation, privileged state, candidates, chosen action and behavior probability. |
| `command` | Command sequence, decision link if applicable, requested IDs, accepted/rejected status, before/after view and accepted command's privileged state after execution. |
| `round_result` | Final attacking score including bottom, winning team, upgrade gain and dealer transition. |
| `decision_outcome` | Terminal round reward linked to each accepted decision or bidding wait. |
| `match_result` | Match champion and final levels, when using `--match`. |
| `run_end` | Completed-round count and final phase. |

A `decision` contains:

- `observation`: the exact `AIContext` given to the policy. Own hand, trump/level,
  dealer, score, other players' card counts, current trick, **all completed
  tricks**, bid history, and bottom cards known to the dealer. Cards are objects
  with `id`, `suit`, `rank`.
- `privileged.hands`: all four current hands in seat order, immediately before
  this decision. `privileged.bottom` and `privileged.draw_pile` preserve the
  remaining hidden state. The draw pile is in engine order: the next card is
  popped from its end. During burying, the bottom is temporarily in the dealer's
  hand and `bottom` is empty.
- `candidates`: highest-ranked first. Each has `index`, `action`, `ids`, card
  objects, policy `score`, scoring `reasons`, and `selection_probability`.
- `chosen`: action kind, requested card IDs, and `candidate_index`.
- `behavior`: `epsilon_greedy` or `external`, epsilon, whether the random branch
  was used, and the chosen candidate's selection probability.
- `execution`: `command` for moves sent to the engine, or `wait` for an AI's
  decision not to declare while considering its currently drawn hand.

The safe observation and privileged snapshot are separate on purpose. Use only
`observation` plus candidate information for an ordinary player policy. The
all-hands snapshot is available for teacher rollouts, a privileged critic, or
debugging; it is never supplied to `AIStrategy.rank`.

Each accepted play command also records `actual_play`: the cards actually removed
from the hand. A failed throw can make this differ from the requested IDs.
`completed_trick` records the four plays, winner and points when that command
finishes a trick. All accepted commands include `privileged_after`.

For external `sim.command(seat, ...)` calls, the configured policy's candidates
and scores are recorded for comparison; they do not choose the external action.
An external action can be absent from the bounded candidate set, in which case
`chosen.candidate_index` is null. Its behavior probability is also null. Rejected
commands retain their error and never receive a training reward.

Bidding waits during incremental dealing are logged as decisions without an
engine command, preserving the simulator's existing timing-free bidding behavior.
Controls such as drawing and starting a round have command records but no
decision or candidates.

## Read completed training samples

```python
from levelup.training import iter_decisions

for sample in iter_decisions("logs/training/selfplay-000.jsonl.gz"):
    if sample["observation"]["phase"] != "playing":
        continue
    observation = sample["observation"]
    candidates = sample["candidates"]
    chosen_index = sample["chosen"]["candidate_index"]
    actual_cards = sample["transition"]["actual_play"]
    all_hands = sample["privileged"]["hands"]
    reward = sample["outcome"]["reward"]
    # Encode (observation, candidate) for the ranker; group by decision_id.
```

The reader joins decisions to accepted transitions and outcomes while streaming
rounds. Waiting decisions have `transition=None`. Rejections and unfinished
rounds are excluded from its output; their raw records remain in the file.

Rewards are `+1` if the acting player's team wins the round and `-1` otherwise.
The outcome also records attacking score and upgrade gain; match outcomes can be
joined by `run_id`. An unfinished round receives no fabricated outcome.

**Only the chosen move has an observed return.** Unchosen candidates have policy
scores, not counterfactual value labels. Use those scores for imitation, or add
rollouts to estimate alternative values. Do not copy the chosen move's terminal
reward onto every candidate as though each alternative had been played.
Split evaluation data by whole game/seed, not candidate rows from the same game.

Replaying all accepted `command` records from `Game(run_start.seed)` reconstructs
the game independently of subsequent policy changes, provided the rules engine
matches. Directly mutating `sim.game` outside `sim.command` is not replayable from
the command log alone.
