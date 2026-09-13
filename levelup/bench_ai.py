"""Reproducible paired-deal comparison; not a statistical strength guarantee."""
import argparse
import json
from pathlib import Path
import statistics
import time

from .game import Game
from .ai.simulation import finish_ai_deal
from .ai import strategy_catalog


def benchmark(seeds, advanced="rule_based", baseline="basic", seed_start=0):
    if seeds < 1 or advanced == baseline:
        raise ValueError("Choose at least one seed and two different strategies")
    outcomes, timings = [], {baseline: [], advanced: []}
    for seed in range(seed_start, seed_start + seeds):
        # Repeat the same deal with the strategies swapping partnerships.
        for advanced_team in (0, 1):
            game = Game(seed)
            game.start()
            decisions = 0
            while game.phase not in ("round_end", "match_end"):
                if game.phase == "dealing":
                    finish_ai_deal(game, tuple(advanced if i % 2 == advanced_team else baseline for i in range(4)))
                    continue
                strategy = advanced if game.turn % 2 == advanced_team else baseline
                start = time.perf_counter()
                action, ids = game.ai_action(game.turn, strategy)
                timings[strategy].append(time.perf_counter() - start)
                game.act(game.turn, action, ids)
                decisions += 1
                if decisions > 250:
                    raise RuntimeError(f"Round stalled: seed={seed}, advanced_team={advanced_team}")
            outcomes.append({"seed": seed, "advanced_team": advanced_team,
                             "advanced_won": game.result["team"] == advanced_team,
                             "attacker_score": game.score, "dealer": game.dealer,
                             "gain": game.result["gain"]})
    wins = sum(r["advanced_won"] for r in outcomes)
    return {"seeds": seeds, "seed_start": seed_start, "advanced": advanced, "baseline": baseline,
            "paired_rounds": len(outcomes), f"{advanced}_wins": wins,
            f"{advanced}_win_rate": wins / len(outcomes),
            "decision_ms": {key: {"median": round(statistics.median(values) * 1000, 3),
                                  "p95": round(sorted(values)[int(0.95 * (len(values) - 1))] * 1000, 3),
                                  "max": round(max(values) * 1000, 3)} for key, values in timings.items()},
            "note": "Incremental draws with immediate AI reactions (no wall-clock delays), swapped partnerships. Decision timings cover bury/play only. Not proof of general playing strength.",
            "rounds": outcomes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", type=int, default=30)
    choices = [strategy['id'] for strategy in strategy_catalog()]
    parser.add_argument("--advanced", choices=choices, default="rule_based")
    parser.add_argument("--baseline", choices=choices, default="basic")
    parser.add_argument("--seed-start", type=int, default=0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.seeds < 1:
        parser.error("--seeds must be positive")
    if args.advanced == args.baseline:
        parser.error("Choose two different strategies")
    result = benchmark(args.seeds, args.advanced, args.baseline, args.seed_start)
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "rounds"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
