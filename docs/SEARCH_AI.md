# Search AI: a 30-second move budget

Select **搜索 AI（最多 30 秒）**, strategy ID `search`, when creating a room or
between rounds. Bidding and burying use the existing rule-based strategy. The
default room strategy remains `rule_based`.

Search receives only `AIContext`: own cards, public history, and the bottom if this
player is the dealer. It never receives the actual opponents' hands.

## Algorithm

1. Collapse interchangeable copies of each legal candidate. Return immediately
   when there is just one candidate.
2. Shortlist up to eight actions: three rule-based leaders, two XGBoost leaders,
   one random candidate, then additional rule-based candidates. Skip duplicates.
3. Sample hidden holdings with the exact public counts and eight bottom cards.
   Respect void suits and exposed bid cards; a dealer's exposed card may have been
   buried. Reverse public plays to reconstruct earlier hands and reject assignments
   violating observed follow-suit, pair, tractor, or successful-throw obligations.
4. Compare every active candidate on the **same sampled deal**, playing to the end
   of the round. Each simulated player uses its own information and a rule-based
   policy; one in four worlds uses a small rule-weight variation.
5. After 16 complete comparisons, successively halve the candidates toward two
   finalists. Keep the rule-based move as a reference throughout. Stop after 48
   completed worlds or the deadline.
6. With at most three cards per player, try exhaustive minimax on the sampled
   deal, capped at 1,500 nodes with deadline checks. On node-budget exhaustion,
   evaluate every candidate with the same public-information rollout policy.

Round utility is
`win + 0.002 × signed_potential_level_gain + 0.00001 × signed_attacking_points`.
Small secondary terms distinguish similar winning frequencies. Potential level
gain uses score thresholds before any cap at A. This is not calibrated full-match
winning probability.

The candidate space is bounded. Random matching and capacity-preserving swaps
produce an approximate belief, not uniform posterior samples or an opponent model
of bidding and burial preferences. Exact endgame values apply only to sampled
deals, not the actual hidden game. This is sampled rollout search, not full
information-set MCTS. Sampling failure falls back to rules without relaxing the
public-information constraints.

## Runtime and controls

The coordinator stops at 28 seconds, reserving time to submit within 30 seconds.
Shorter room clocks shorten that budget. Forced decisions can finish immediately.
Expired human clocks use a rule-based move instead of starting another search.

The web server coordinates in a thread and simulates in a shared pool of up to
eight processes with bounded queued work. It holds no room lock while searching,
and also enforces an outer timeout. A game/version/turn/strategy signature prevents
stale results from playing. Changes of turn and abandoned rooms cancel coordinators;
already executing workers stop at their deadline checks.

The table shows when the AI is thinking. Search hints leave manual selection and
play available; a late hint cannot overwrite an outstanding manual play request.
Restart your existing server and refresh the browser to load the new strategy.

## Offline simulation and training records

```bash
~/.venv/bin/python -m levelup.simulation --games 1 --seed 43000 \
  --strategies search rule_based search rule_based \
  --training --output logs/training/search-selfplay.jsonl.gz
```

Training decisions retain the existing safe observation, privileged actual hands,
candidates, command, and outcome. Search rankings add a `search` record: budget,
elapsed time, completed worlds, sampling attempts, exact-world counts, and each
shortlisted candidate's sample count, mean utility, and elimination status.
Unsearched/eliminated candidates have lower **ordering scores**, not value targets.
Use separately recorded mean utility and sample counts for future training, while
accounting for pruning and finite-sample selection bias.

World seeds derive from the observation; wall-clock cutoffs can change completed
sample counts and actions across machines. No native ML library is needed at runtime.

`SearchStrategy(seconds=28, max_worlds=48).search(context, candidates)` returns
`SearchResult(ranked, report)`. `rank(context, candidates)` preserves the existing
strategy interface. In standalone Python scripts, put process-spawning calls under
an `if __name__ == '__main__':` guard; `shutdown_search()` releases the worker pool.

## Reproduce the benchmark

```bash
~/.venv/bin/python -m levelup.bench_search \
  --seeds 10 --seed-start 41000 --parallel 3 \
  --output logs/search-benchmark-v1
```

This runs 20 first-round games, swapping partnerships on each of 10 fresh seeds.
Both sides use identical bidding and burial policies. Each parallel round has up
to eight simulation workers; `--parallel 3` can use 24 workers. Use `--parallel 1`
and a fresh output directory for a serial run. Caps remain per-move wall-clock
budgets, including waiting for workers.

Completed rounds are checkpointed and decisions written to JSONL traces. The final
`summary.json` contains results, latency, world/fallback counts, and a paired sign
test on swept versus lost seed pairs. `config.json` records parameters and a
source/model fingerprint; resuming with changed settings or code is rejected.
This small first-round benchmark alone cannot establish general match or human strength.

## Version 1 result (2026-09-11)

The frozen configuration above won **14 of 20 rounds (70%)** against `rule_based`.
Across the 10 paired seeds, search swept four pairs, split six, and lost no pair
outright. The exact two-sided paired sign-test p-value is **0.125**: this is a
promising observed advantage, not statistically conclusive evidence of general
superiority. Neither parameters nor policies were tuned during this evaluation.

| Measurement | Result |
| --- | ---: |
| Search-side play decisions | 712 |
| Decisions receiving simulations | 604 |
| Completed sampled-deal comparisons | 28,992 |
| Sampling-failure fallbacks | 0 |
| Median move time | 3.71 seconds |
| p95 move time | 13.13 seconds |
| Slowest move | 20.75 seconds |

Every simulated decision completed its 48-world limit within the 28-second search
cap. The other 108 decisions had one distinct candidate. Timings include candidate
generation and forced/single-candidate moves, with three rounds running concurrently
on the development machine. All 20 games were independently replayed from their
seeds and recorded search moves; observations, scores, and winners matched exactly.

Full results and the tested source/model fingerprint are in
[search-benchmark-v1.json](search-benchmark-v1.json). Per-decision traces and resumable
checkpoints remain in `logs/search-benchmark-v1/`. Validation also passed 221 Python
tests and 23 client tests, plus installed-wheel search with Python `-S` and no
third-party ML libraries.
