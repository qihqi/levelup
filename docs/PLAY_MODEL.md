# Play-only XGBoost AI

Select **XGBoost 出牌 AI** in the room's AI selector. Its strategy ID is
`xgboost_play`; it also works with offline simulation, hints, and automatic takeover.
Declaring trump and burying cards delegate to `RuleBasedStrategy` unchanged.
The default room strategy remains `rule_based`.

The bundled model is `levelup/models/play_v1.json`, with its dataset fingerprint,
feature schema, teacher methods, and held-out metrics in `play_v1.meta.json`.
Both files ship in the wheel. Inference uses a small Python evaluator of the
exported numeric trees, so playing and offline simulation require no XGBoost or
NumPy installation. Training checks its predictions against native XGBoost.

## What a label means

We collected 28 independent first-round games with seeds 1000–1027, four
`rule_based` players, and 15% candidate exploration. The first 2,000 **playing**
decisions form this dataset. Each record retains the observation, all four actual
hands and bottom cards, every ranked candidate, the behavior action, and the
teacher's label and scores. These privileged records stay in local `logs/training/`;
they are never sent to a browser or passed to the deployed strategy.

The teacher evaluates each candidate from the same full-information state:

* If there is only one legal face-multiset play, the decision is marked `forced`.
* When everyone has at most three cards, it attempts exhaustive partnership
  minimax over all legal plays, including combinations outside the usual candidate
  generator. A 30,000-node budget bounds each decision's search.
* Otherwise, or if that budget is exhausted, it plays every candidate through to
  the end of the round under two deterministic full-information rule policies.
  Their mean final score is the candidate's estimated value. These policies know
  actual holdings, voids, and the bottom, and can make safe full-suit throws.

The objective is final attacking points including bottom multipliers: the attacking
partnership maximizes it and the defending partnership minimizes it. Both partners
cooperate. This is a round-score objective, not match-winning probability or level gain.
All tied best candidates receive positive labels; the exported `best_play` is one
representative. `candidate_values`, `best_candidate_indices`, `method`, and
`proven_optimal` make the distinction inspectable.

**Most labels are estimates, not proofs of the absolute best play.** Exact-search
labels are marked proven only if a supplied candidate attains the global minimax
value. Root candidate generation is bounded, so some legal plays can be absent.
Forced decisions are retained for audit but do not provide a learning signal.

## Student model and evaluation

`AIStrategy.rank(context, candidates)` remains the interface. The shared encoder
uses only `AIContext`: own hand, public plays and declarations, public card counts,
inferred void suits, and bottom cards if this player is the dealer. Features include
card-strength histograms, action size/points/trumps, and individual rule-based
scoring terms. Physical card IDs and privileged hands are not model inputs.
The teacher can use more information than the student; identical public information
can therefore receive different labels in different hidden deals.

Each decision is one XGBoost ranking group. The objective is `rank:pairwise`,
with relevance 1 for teacher-best candidates and 0 for the rest. Forced and all-tied
groups are excluded from training. Trees have depth at most 3; the tree count is
chosen from 40, 80, 120, 160, and 220 on validation games. Whole games are split
between train, validation, and test with fixed seed 20260911, preventing adjacent
states from the same game leaking across splits. Test games do not select the model.
Agreement and estimated point regret are reported on decisions with a meaningful
choice, excluding all-tied groups.

This is a small first experiment. The collection contains only level-2 opening
rounds; play at other levels, later matches, and unfamiliar policies needs more data.
Teacher agreement alone does not establish stronger match play. Use the paired
benchmark below on fresh seeds and report its sample size alongside any win rate.

## Version 1 results

The bundled model has **160 trees, depth at most 3, 143 features**, and its JSON
weights occupy **195,185 bytes**. The 2,000 decisions contain 19,892 candidate rows:
1,584 decisions use full-information rollout estimates, 109 use exhaustive minimax,
and 307 are forced. Of the minimax decisions, 107 have a supplied candidate that
attains the global optimum; two have a better legal play outside the candidate set.
Thus 414 labels are proven optimal, including the 307 forced moves.

| Split | Independent games | Decisions | Nontrivial decisions | Model teacher agreement | Rule-based agreement |
| --- | ---: | ---: | ---: | ---: | ---: |
| Train | 20 | 1,440 | 894 | 60.0% | 45.6% |
| Validation | 4 | 244 | 167 | 50.9% | 47.9% |
| Test | 4 | 316 | 209 | 51.7% | 51.2% |

On test decisions, average teacher-estimated point regret is 9.04 for the model
and 8.92 for the rule-based policy. These are teacher-estimated counterfactual
scores, not realized match results. Portable predictions differ from native
XGBoost by at most 0.000000507 on the held-out candidates.

In the fresh-deal comparison (seeds 10000–10009, partnerships swapped), the model
won **7 of 20 rounds (35%)** against `rule_based`. Median decision time was 3.72 ms
and p95 was 19.79 ms on the development machine, including feature extraction
and candidate generation; timings cover bury/play. See the complete
[benchmark results](play-model-v1-benchmark.json).

**This version does not demonstrate stronger play than the rule-based AI.** It is
a working experimental strategy and a reproducible training pipeline, with the
rule-based AI retained as the default. More diverse data and a stronger teacher
are the next experiments; neither model size nor features were tuned on the test
games or this fresh-deal benchmark.

## Reproduce or gather the next iteration

From the repository root:

```bash
uv pip install --python "$HOME/.venv/bin/python" -e '.[train]'

~/.venv/bin/python -m levelup.simulation --games 28 --seed 1000 \
  --training --exploration 0.15 \
  --output logs/training/xgb-v1-selfplay.jsonl.gz

~/.venv/bin/python -m levelup.train_play label \
  logs/training/xgb-v1-selfplay.jsonl.gz \
  logs/training/xgb-v1-labeled.jsonl.gz --limit 2000 --workers 8

~/.venv/bin/python -m levelup.train_play train \
  logs/training/xgb-v1-labeled.jsonl.gz

~/.venv/bin/python -m levelup.bench_ai --advanced xgboost_play \
  --baseline rule_based --seed-start 10000 --seeds 10 \
  --output logs/training/xgb-v1-benchmark.json
```

Use fresh output names when changing the teacher, source data, or rollout settings.
The simulator appends runs; the labeler can resume a valid, closed gzip output with
a matching decision-ID prefix. An interrupted write may leave an incomplete gzip
stream; use a fresh label file in that case. Multiprocessing workers use separate
game objects and never start a web server. The benchmark repeats each seed with
the strategies swapping partnerships and checks that every round terminates.

For subsequent self-play, pass
`--strategies xgboost_play xgboost_play xgboost_play xgboost_play` to the simulator,
retain exploration, and add new seeds (ideally full matches and diverse opponents).
Relabel those decisions before fitting another version. Model scores stored by the
simulator are behavior scores, not counterfactual value labels.

Implementation: `levelup/god_teacher.py`, `levelup/train_play.py`,
`levelup/ai/play_features.py`, and `levelup/ai/xgboost_play.py`.
See also [training record schema](TRAINING_DATA.md) and XGBoost's official
[learning-to-rank documentation](https://xgboost.readthedocs.io/en/release_3.2.0/tutorials/learning_to_rank.html).
