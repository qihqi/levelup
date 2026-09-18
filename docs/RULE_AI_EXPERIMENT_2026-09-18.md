# Rule-based AI challenger: research and paired evaluation

The incumbent is unchanged in `levelup/ai/rule_based.py`. The temporary fork is
`levelup/ai/rule_based_trial.py`; it is **not registered or offered in the web UI**.
Only play scoring changes. Bidding, burying, legal candidates, and game rules
remain identical, so the experiment isolates playing strength.

**Decision: retain the incumbent.** The frozen challenger won 504/1,024 held-out
rounds (49.22%; paired 95% interval 46.97–51.46%). This is compatible with equal
strength and does not establish an improvement or a statistically clear decline.

## Research and proposed improvements

Sources reviewed on 2026-09-18:

- [ShengJi+: Playing Tractor with Deep Reinforcement Learning, Berkeley, 2023](https://www2.eecs.berkeley.edu/Pubs/TechRpts/2023/Archive/EECS-2023-127.pdf),
  especially the worked play examples in Appendix 7.1: coordinate point feeding,
  take control at an appropriate cost, avoid drawing only your partner's trumps,
  and consider the endgame value of retained patterns. Its rules include
  variations absent from our game; those variations are not imported.
- [IceKing, 拖拉机牌理漫谈](https://tractorcardgame.com/2021/01/tractor_survival_guide/):
  purposeful trump draws, helping a partner regain the lead, preserving entries,
  and planning around scoring thresholds. This is a **six-player** guide; its
  score thresholds, bidding conventions and signaling agreements do not apply
  directly to our four-player game. Signals are not treated as facts.
- [Pagat: Tractor](https://www.pagat.com/kt5/tractor.html): checked the distinction
  between following suit, playing matching patterns and ruffing. Our own engine
  remains authoritative for legality and scoring.

Concrete experiments:

1. **Count threats instead of assigning fixed risk.** The incumbent assigns
   fixed 48%/60% danger when any higher same-suit pattern could exist. The trial
   estimates higher-card ownership from unseen cards and remaining hand sizes.
   Exposed cards in a non-dealer's hand are fixed; the dealer may have buried an
   exposed card. A higher trump threatens an existing ruff only if its owner can
   legally avoid following the original suit.
2. **Remember exhausted pairs.** If a player must follow a pair-containing lead
   but supplies no pair, that suit cannot contain a pair in their remaining hand.
   This affects estimated threat, not the shared legality/candidate generator.
3. **Preserve useful entries.** Penalize throwing away a known side-suit master
   when another player wins. Avoid drawing trump when neither opponent can hold
   one; discourage pair leads into a partner's known void.
4. **Include the partner still to act.** Estimate the chance that a later partner
   beats the current enemy winner and any remaining enemy response. The old
   scorer treats the points as lost until someone on its team currently leads.
   Equal cards respect seat order. Small-deck exhaustive tests check this logic.
5. **Test initiative separately.** Winning a zero-point trick can be worthwhile
   when it gives access to cashable side aces. Another variant rewards that and
   reduces rigid pair preservation near the end. It was not automatically assumed
   stronger merely because the reasoning sounded plausible.

These probabilities are heuristic estimates, not calibrated win probabilities.
Different concealed hands are approximated independently. Pair/tractor events
can overlap; their union uses an approximation. Composite throws are assessed
through their largest component. No variant reads actual opponent hands or uses
an LLM, trained model, or sampled-deal search.

## Evaluation protocol fixed before the held-out run

- Original strategy completes bidding and burying once per seed.
- Both comparisons start from deep copies of that same post-bury state.
- New AI plays seats 0/2, then seats 1/3; the incumbent occupies the opposite team.
- Levels cycle through 2–A and the initial dealer rotates across seats.
- Development: seeds **1000–1255**, 256 pairs / 512 rounds per variant.
  A preliminary 64-seed run was a subset of these seeds, not extra evidence.
- Select one version using development results; then freeze it before opening
  fresh seeds **50000–50511**, 512 pairs / 1,024 held-out rounds.
- The 95% interval is a deterministic 10,000-resample **seed-pair bootstrap**.
  Swapped games from one deal are correlated and are not counted as independent.
- Promotion requires a held-out run of at least 256 pairs, **win rate ≥55%**, and
  **95% interval lower bound >50%**. Development results cannot authorize promotion.
- This measures round wins against the incumbent, not full-match wins or strength
  against human players. Latency includes observation, candidate generation and
  ranking, and excludes dealing, burying and applying the command.

## Development results

| Variant | Trial wins / 512 | Win rate | Paired 95% interval |
| --- | ---: | ---: | --- |
| Counting only | 259 | 50.59% | 48.24–52.93% |
| Tactical bonuses with original risk estimates | 264 | 51.56% | 49.22–54.10% |
| Counting + tactical bonuses | 264 | 51.56% | 48.44–54.69% |
| Above + later-partner recovery | 267 | 52.15% | 48.83–55.47% |
| Above + initiative/endgame changes | 267 | 52.15% | 48.83–55.47% |

Selected `partnership`, breaking the tie by its better average signed points
(+1.65 versus +1.13) and lower latency. Each row uses the **same** development
deals; these are comparisons of variants, not independent confirmations.

## Held-out result and decision

| Measure | Result |
| --- | ---: |
| Fresh seeds / swapped rounds | 512 / 1,024 |
| Challenger wins | 504 |
| Challenger round win rate | 49.22% |
| Seed-pair bootstrap 95% interval | 46.97–51.46% |
| Challenger wins as defender / attacker | 55.27% / 43.16% |
| Original median / p95 decision time | 2.23 / 9.03 ms |
| Challenger median / p95 decision time | 3.67 / 14.75 ms |
| Promotion gate | **Failed** |

The role-specific percentages are descriptive: defending and attacking do not
have equal inherent win chances, which is why every seed swaps policy teams.
The live registry, default strategy, and original implementation were not changed.
No user server was restarted. The challenger remains an opt-in code experiment.
All 86 selected AI, model-fallback, search, packaging and new experiment tests pass.

The improvements are locally plausible but their combined weights do not produce
a reliable advantage. Further experiments should first measure probability
calibration against logged outcomes, then consider remaining winners/entries and
last-trick control together rather than stacking more independent bonuses. Any
future tuning must use a **new** held-out seed range; this range is now evaluation
history, not fresh validation data. Full-match and mixed human/AI partnership
evaluation would be additional checks before claiming broader playing strength.

## Reproduce

From the repository, using the existing shared Python environment:

```bash
~/.venv/bin/python -m levelup.bench_rules \
  --variant partnership --split development \
  --seeds 256 --seed-start 1000 --workers 4 \
  --output /tmp/rules-development.json

~/.venv/bin/python -m levelup.bench_rules \
  --variant partnership --split holdout \
  --seeds 512 --seed-start 50000 --workers 4 \
  --output /tmp/rules-holdout.json

~/.venv/bin/python -m pytest tests/test_rule_based_trial.py -q
```

Raw per-round results and source hashes are in [`benchmarks/`](benchmarks/).
The earlier development hashes differ because additional, opt-in variants were
added during development. The final module still supports each named variant.
`passes_gate` in earlier development artifacts describes numerical criteria only;
the final harness also requires `split=holdout` for `eligible_for_promotion`.
The harness never changes the live strategy registry.
