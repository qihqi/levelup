"""Checkpointed paired-round search-vs-rule benchmark with per-decision diagnostics."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from hashlib import sha256
import json
import math
import multiprocessing
from pathlib import Path
import statistics
import time

from .ai import observe
from .ai.candidates import generate_candidates
from .ai.search import SearchStrategy, WORKERS, shutdown_search
from .ai.simulation import finish_ai_deal
from .game import Game


def _round_job(seed, team, options, directory):
    directory = Path(directory)
    result_path = directory / f'{seed}-{team}.json'
    game = Game(seed)
    game.start()
    strategy = SearchStrategy(**options)
    strategies = tuple('search' if seat % 2 == team else 'rule_based' for seat in range(4))
    moves, timings, reports = 0, [], []
    start = time.monotonic()
    with (directory / f'{seed}-{team}.jsonl').open('w') as log:
        while game.phase not in ('round_end', 'match_end'):
            if game.phase == 'dealing':
                finish_ai_deal(game, strategies)
                continue
            seat = game.turn
            if game.phase == 'playing' and seat % 2 == team:
                ctx = observe(game, seat)
                before = time.monotonic()
                result = strategy.search(ctx, generate_candidates(ctx))
                timings.append(time.monotonic() - before)
                reports.append(result.report)
                action = result.ranked[0].action
                kind, ids = action.kind, [c.id for c in action.cards]
                log.write(json.dumps({'move': moves, 'seat': seat, 'observation': asdict(ctx),
                                      'action': kind, 'ids': ids, 'search': result.report}) + '\n')
                log.flush()
            else:
                kind, ids = game.ai_action(seat, 'rule_based')
            game.act(seat, kind, ids)
            moves += 1
            if moves > 250:
                raise RuntimeError(f'Round did not finish: {seed}, team={team}')
    result = {'seed': seed, 'search_team': team, 'search_won': game.result['team'] == team,
              'result': game.result, 'moves': moves, 'seconds': time.monotonic()-start,
              'search_decisions': len(reports), 'completed_worlds': sum(r['worlds'] for r in reports),
              'fallbacks': sum(r['reason']=='no_complete_comparison' for r in reports),
              'decision_seconds': timings}
    temporary = result_path.with_suffix('.pending')
    temporary.write_text(json.dumps(result, indent=2)+'\n')
    temporary.replace(result_path)
    return result


def round_job(seed, team, options, directory):
    try:
        return _round_job(seed, team, options, directory)
    finally:
        shutdown_search()


def summarize(results, options, parallel):
    wins = sum(r['search_won'] for r in results)
    n = len(results)
    paired = {}
    for r in results:
        paired.setdefault(r['seed'], []).append(r['search_won'])
    complete = [sum(v) for v in paired.values() if len(v) == 2]
    better, worse = complete.count(2), complete.count(0)
    decisive = better + worse
    # Exact two-sided sign test on paired deal outcomes; tied pairs carry no evidence.
    p = min(1.0, 2*sum(math.comb(decisive,k) for k in range(min(better,worse)+1))/2**decisive) if decisive else 1.0
    times = sorted(t for r in results for t in r['decision_seconds'])
    return {'rounds': n, 'search_wins': wins, 'search_win_rate': wins/n if n else None,
            'paired_seeds': len(complete), 'pairs_search_swept': better, 'pairs_rule_swept': worse,
            'pairs_split': complete.count(1), 'paired_sign_test_two_sided_p': p,
            'options': options, 'parallel_rounds': parallel, 'simulation_workers_per_round': WORKERS,
            'search_decisions': len(times), 'completed_worlds':sum(r['completed_worlds'] for r in results),
            'fallbacks':sum(r['fallbacks'] for r in results),
            'decision_seconds': {'median':statistics.median(times),
                                 'p95':times[int(.95*(len(times)-1))], 'max':max(times)} if times else None,
            'note':'Fresh first-round seeds, swapped partnerships, same rule-based bidding/burying. Search cap is 28s plus submission allowance. Not a full-match or human-strength guarantee.',
            'results':results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seeds', type=int, default=10)
    parser.add_argument('--seed-start', type=int, default=41000)
    parser.add_argument('--parallel', type=int, default=1)
    parser.add_argument('--seconds', type=float, default=28)
    parser.add_argument('--worlds', type=int, default=48)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.seeds < 1 or args.parallel < 1:
        parser.error('seeds and parallel must be positive')
    options = {'seconds':args.seconds, 'max_worlds':args.worlds, 'shortlist':8}
    SearchStrategy(**options)
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).parent
    sources = [root/'game.py', root/'bench_search.py', *sorted((root/'ai').glob('*.py')),
               root/'models/play_v1.json']
    digest = sha256()
    for path in sources:
        digest.update(path.relative_to(root).as_posix().encode()+b'\0'+path.read_bytes())
    config = {'seed_start':args.seed_start,'seeds':args.seeds,'parallel':args.parallel,
              'options':options,'source_sha256':digest.hexdigest()}
    manifest = args.output/'config.json'
    if manifest.exists() and json.loads(manifest.read_text()) != config:
        raise ValueError('Output belongs to a different benchmark configuration/code; choose a new directory')
    manifest.write_text(json.dumps(config,indent=2)+'\n')
    jobs, results = [], []
    for seed in range(args.seed_start,args.seed_start+args.seeds):
        for team in (0,1):
            path = args.output/f'{seed}-{team}.json'
            if path.exists():
                results.append(json.loads(path.read_text()))
            else:
                jobs.append((seed,team))
    with ProcessPoolExecutor(args.parallel,mp_context=multiprocessing.get_context('spawn')) as executor:
        futures = [executor.submit(round_job,seed,team,options,str(args.output)) for seed,team in jobs]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(json.dumps({'completed':len(results),'total':2*args.seeds,
                              'seed':result['seed'],'search_team':result['search_team'],
                              'search_won':result['search_won'],'score':result['result']['score'],
                              'round_seconds':round(result['seconds'],1)}),flush=True)
    results.sort(key=lambda r:(r['seed'],r['search_team']))
    report = {**config,**summarize(results,options,args.parallel)}
    (args.output/'summary.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='results'},indent=2))
    shutdown_search()


if __name__ == '__main__':
    main()
