"""Paired play-only evaluation of the isolated rule-based trial against baseline.

Both policies receive identical post-bury deals and swap partnerships. Bidding
and burying are always performed by the incumbent. Confidence is computed over
seed pairs, not incorrectly over twice as many independent games.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import random
import statistics
import time

from .ai import observe
from .ai.candidates import generate_candidates
from .ai.rule_based import RuleBasedStrategy
from .ai.rule_based_trial import TrialRuleBasedStrategy
from .ai.simulation import finish_ai_deal
from .game import Game


def paired_seed(job):
    seed, variant = job
    # Registration is process-local and never exposes the trial to the web UI.
    incumbent, trial = RuleBasedStrategy(), TrialRuleBasedStrategy(variant=variant)
    game = Game(seed)
    level = 2 + seed % 13
    game.levels = [level, level]
    game.dealer = seed % 4
    game.start()
    finish_ai_deal(game)
    while game.phase == "burying":
        action, ids = game.ai_action(game.turn, "rule_based")
        game.act(game.turn, action, ids)
    results = []
    for trial_team in (0, 1):
        played = deepcopy(game)
        timings = {"baseline": [], "trial": []}
        decisions = 0
        while played.phase == "playing":
            is_trial = played.turn % 2 == trial_team
            strategy = trial if is_trial else incumbent
            start = time.perf_counter()
            ctx = observe(played, played.turn)
            ranked = strategy.rank(ctx, generate_candidates(ctx))
            timings["trial" if is_trial else "baseline"].append(time.perf_counter() - start)
            move = ranked[0].action
            played.act(played.turn, move.kind, [c.id for c in move.cards])
            decisions += 1
            if decisions > 100:
                raise RuntimeError(f"Stalled seed {seed}")
        results.append({"seed": seed, "level": level, "trial_team": trial_team,
                        "trial_defending": game.dealer % 2 == trial_team,
                        "trial_won": played.result["team"] == trial_team,
                        "attacker_score": played.score, "gain": played.result["gain"],
                        "signed_points": (80 - played.score if game.dealer % 2 == trial_team
                                          else played.score - 80),
                        "timings": timings})
    return results


def summarize(rounds):
    pairs = {}
    timings = {"baseline": [], "trial": []}
    for row in rounds:
        pair = pairs.setdefault(row["seed"], {})
        team = row["trial_team"]
        if team not in (0, 1) or team in pair:
            raise ValueError("Each seed must swap the two partnerships exactly once")
        pair[team] = int(row["trial_won"])
        for name, values in row.get("timings", {}).items():
            timings[name].extend(values)
    if any(len(values) != 2 for values in pairs.values()):
        raise ValueError("Each seed must have both partnership assignments")
    scores = [sum(v.values()) / 2 for v in pairs.values()]
    mean = statistics.mean(scores)
    rng = random.Random(20260918)
    resamples = sorted(statistics.mean(rng.choices(scores, k=len(scores))) for _ in range(10000))
    ci = [resamples[250], resamples[9749]]
    return {"seeds": len(scores), "rounds": len(rounds),
            "wins": sum(row["trial_won"] for row in rounds), "win_rate": mean,
            "paired_bootstrap_95_ci": ci,
            "promotion_gate": {"minimum_seeds": 256, "minimum_win_rate": .55,
                               "ci_lower_above": .5},
            "passes_gate": len(scores) >= 256 and mean >= .55 and ci[0] > .5,
            "by_role": {role: statistics.mean(int(r["trial_won"]) for r in rounds
                                              if r["trial_defending"] == defending)
                        for role, defending in (("defending", True), ("attacking", False))},
            "mean_signed_points": statistics.mean(r["signed_points"] for r in rounds),
            "decision_ms": {name: {"median": 1000 * statistics.median(values),
                                    "p95": 1000 * sorted(values)[int(.95 * (len(values) - 1))]}
                            for name, values in timings.items() if values}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", type=int, default=64)
    parser.add_argument("--seed-start", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--split", choices=("development", "holdout"), default="development")
    parser.add_argument("--variant", choices=("full", "counting", "tactics", "partnership", "initiative"), default="partnership")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.seeds < 2 or args.workers < 1:
        parser.error("Use at least two seeds and one worker")
    paths = ("rule_based.py", "rule_based_trial.py", "knowledge.py", "candidates.py")
    hashes = {name: sha256((Path(__file__).parent / "ai" / name).read_bytes()).hexdigest()
              for name in paths}
    started = time.perf_counter()
    rounds = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        jobs = pool.map(paired_seed, ((seed, args.variant) for seed in
                                    range(args.seed_start, args.seed_start + args.seeds)))
        for i, result in enumerate(jobs, 1):
            rounds.extend(result)
            if i % 16 == 0:
                wins = sum(r["trial_won"] for r in rounds)
                print(f"{i}/{args.seeds} pairs: {wins}/{len(rounds)} wins", flush=True)
    summary = summarize(rounds)
    result = {"protocol": "same post-bury deal; swapped teams; baseline bidding/burying; all levels",
              "seed_start": args.seed_start, "variant": args.variant, "source_sha256": hashes,
              "split": args.split, "eligible_for_promotion": args.split == "holdout" and summary["passes_gate"],
              "elapsed_seconds": time.perf_counter() - started, **summary,
              "results": [{k: v for k, v in r.items() if k != "timings"} for r in rounds]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "results"}, indent=2))


if __name__ == "__main__":
    main()
