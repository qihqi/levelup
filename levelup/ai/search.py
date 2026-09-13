"""Deadline-bounded sampled-deal search, guided by rules and XGBoost."""
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from copy import copy
from dataclasses import dataclass
from hashlib import sha256
from itertools import combinations
import multiprocessing
import os
import random
import threading
import time

from ..game import RuleError
from .belief import DealSampler
from .candidates import generate_candidates
from .interface import AIStrategy, Action, RankedAction, Reason, ordered
from .rule_based import RuleBasedStrategy
from .xgboost_play import XGBoostPlayStrategy

WORKERS = min(8, os.cpu_count() or 1)
_pool = None
_pool_lock = threading.Lock()
_slots = threading.BoundedSemaphore(WORKERS * 2)


def pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ProcessPoolExecutor(WORKERS, mp_context=multiprocessing.get_context('spawn'))
        return _pool


def shutdown_search():
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown(wait=False, cancel_futures=True)
            _pool = None


def clone(game):
    child = copy(game)
    child.hands = [list(h) for h in game.hands]
    child.trick, child.history = list(game.trick), list(game.history)
    child.events, child.levels = list(game.events), list(game.levels)
    return child


def utility(game, team):
    won = game.result['team'] == team
    score = game.score
    gain = 3 if score == 0 else 2 if score < 40 else 1 if score < 80 else (score - 80) // 40
    signed_points = -score if team == game.dealer % 2 else score
    return float(won) + .002 * (gain if won else -gain) + .00001 * signed_points


class SearchExpired(Exception):
    pass


class Endgame:
    """Exact only for a sampled deal; never a proof about the hidden real deal."""
    def __init__(self, team, deadline, node_limit=1500):
        self.team, self.deadline, self.node_limit = team, deadline, node_limit
        self.nodes, self.cache = 0, {}

    def value(self, game):
        if time.monotonic() >= self.deadline:
            raise SearchExpired()
        if game.phase in ('round_end', 'match_end'):
            return utility(game, self.team)
        key = (game.turn, tuple(tuple(sorted(c.id for c in h)) for h in game.hands),
               tuple((s, tuple(c.id for c in cs)) for s, cs in game.trick), game.score)
        if key in self.cache:
            return self.cache[key]
        self.nodes += 1
        if self.nodes > self.node_limit:
            raise SearchExpired()
        hand, rules = game.hands[game.turn], game.rules
        sizes = [len(game.trick[0][1])] if game.trick else range(1, len(hand) + 1)
        seen, values = set(), []
        for size in sizes:
            for cards in combinations(hand, size):
                face = tuple(sorted(c.key for c in cards))
                if face in seen:
                    continue
                seen.add(face)
                try:
                    if game.trick:
                        rules.validate_follow(hand, cards, game.trick[0][1])
                    else:
                        rules.components(cards)
                except RuleError:
                    continue
                child = clone(game)
                child.act(child.turn, 'play', [c.id for c in cards])
                values.append(self.value(child))
        value = (max if game.turn % 2 == self.team else min)(values)
        self.cache[key] = value
        return value


def rollout(game, team, variant, deadline):
    from . import observe
    weights = None if variant % 4 != 3 else {'spend_strength': -.7, 'win_control': 28}
    policy = RuleBasedStrategy(weights)
    for _ in range(108):
        if time.monotonic() >= deadline:
            raise SearchExpired()
        if game.phase in ('round_end', 'match_end'):
            return utility(game, team)
        context = observe(game, game.turn)  # Each simulated player sees only its own information.
        action = policy.rank(context, generate_candidates(context))[0].action
        game.act(game.turn, action.kind, [c.id for c in action.cards])
    raise RuntimeError('Search rollout did not finish')


def evaluate_world(context, actions, seed, variant, deadline):
    """One common hidden deal for every candidate; discard incomplete comparisons."""
    sampler = DealSampler(context)
    game = sampler.sample(random.Random(seed), deadline)
    if game is None:
        return {'values': None, 'attempts': sampler.attempts, 'exact': False}
    values = {}
    solver = Endgame(context.seat % 2, deadline) if max(context.counts) <= 3 else None
    exact = solver is not None
    try:
        # Random ordering prevents a deadline consistently favoring the first action.
        order = list(actions)
        random.Random(seed + 1).shuffle(order)
        for index, action in order:
            child = clone(game)
            child.act(child.turn, action.kind, [c.id for c in action.cards])
            if solver is not None:
                try:
                    values[index] = solver.value(child)
                    continue
                except SearchExpired:
                    if time.monotonic() >= deadline:
                        raise
                    # Re-evaluate every candidate with the same rollout policy.
                    solver, exact, values = None, False, {}
                    break
            values[index] = rollout(child, context.seat % 2, variant, deadline)
        if not values or len(values) != len(actions):
            values = {}
            for index, action in order:
                child = clone(game)
                child.act(child.turn, action.kind, [c.id for c in action.cards])
                values[index] = rollout(child, context.seat % 2, variant, deadline)
    except SearchExpired:
        values = None
    return {'values': values, 'attempts': sampler.attempts, 'exact': exact}


@dataclass
class SearchResult:
    ranked: list[RankedAction]
    report: dict


class SearchRankings(list):
    def __init__(self, result):
        super().__init__(result.ranked)
        self.search_report = result.report


class SearchStrategy(AIStrategy):
    id = 'search'
    name = '搜索 AI（最多 30 秒）'
    description = '结合记牌与 XGBoost 候选，在符合公开信息的模拟手牌中比较续局结果；亮主与扣底沿用记牌策略。'

    def __init__(self, seconds=28.0, max_worlds=48, shortlist=8):
        if not 0 <= seconds <= 28 or max_worlds < 1 or shortlist < 2:
            raise ValueError('Search requires 0–28 seconds, positive worlds, and at least two candidates')
        self.seconds, self.max_worlds, self.shortlist = seconds, max_worlds, shortlist

    def rank(self, context, candidates):
        return SearchRankings(self.search(context, candidates))

    def search(self, context, candidates, *, seconds=None, cancel=None):
        start = time.monotonic()
        budget = self.seconds if seconds is None else min(self.seconds, max(0, seconds))
        deadline = start + budget
        baseline = RuleBasedStrategy().rank(context, candidates)
        report = {'budget_seconds': budget, 'worlds': 0, 'sampling_attempts': 0,
                  'exact_worlds': 0, 'candidates_total': len(candidates), 'reason': 'fallback'}
        if context.phase != 'playing':
            return SearchResult(baseline, {**report, 'reason': 'rule_based_phase', 'elapsed_seconds': time.monotonic() - start})
        # Collapse interchangeable copies before allocating simulation effort.
        unique, faces = [], set()
        for item in baseline:
            face = tuple(sorted(c.key for c in item.action.cards))
            if face not in faces:
                unique.append(item.action)
                faces.add(face)
        if len(unique) == 1 or budget < .05:
            return SearchResult(baseline, {**report, 'reason': 'single_candidate' if len(unique) == 1 else 'no_time',
                                           'elapsed_seconds': time.monotonic() - start})
        rng = random.Random(int.from_bytes(sha256(repr(context).encode()).digest()[:8], 'big'))
        student = XGBoostPlayStrategy().rank(context, candidates)
        selected, selected_faces = [], set()

        def include(action):
            face = tuple(sorted(c.key for c in action.cards))
            if face not in selected_faces and len(selected) < self.shortlist:
                selected.append(action)
                selected_faces.add(face)

        for action in unique[:3]:
            include(action)
        for item in student[:2]:
            include(item.action)
        exploration = list(unique)
        rng.shuffle(exploration)
        for action in exploration[:1]:
            include(action)
        for action in unique:
            include(action)

        active = list(range(len(selected)))
        baseline_index = 0
        rewards = {i: [] for i in active}
        eliminated = set()
        pending = set()
        generation = 0
        stop = lambda: time.monotonic() >= deadline or (cancel is not None and cancel.is_set())
        try:
            while report['worlds'] < self.max_worlds and not stop():
                batch = min(WORKERS, self.max_worlds - report['worlds'])
                futures = []
                for _ in range(batch):
                    if stop() or not _slots.acquire(blocking=False):
                        break
                    try:
                        future = pool().submit(evaluate_world, context,
                                               [(i, selected[i]) for i in active],
                                               rng.getrandbits(64), generation, deadline)
                    except Exception:
                        _slots.release()
                        raise
                    future.add_done_callback(lambda _: _slots.release())
                    futures.append(future)
                    generation += 1
                if not futures:
                    time.sleep(min(.01, max(0, deadline - time.monotonic())))
                    continue
                pending = set(futures)
                completed = []
                while pending and not stop():
                    done, pending = wait(pending, timeout=min(.025, max(0, deadline - time.monotonic())),
                                         return_when=FIRST_COMPLETED)
                    completed.extend(done)
                # Process submission order for reproducibility when the full batch finishes.
                for future in futures:
                    if future not in completed:
                        continue
                    result = future.result()
                    report['sampling_attempts'] += result['attempts']
                    if result['values'] is None:
                        continue
                    report['worlds'] += 1
                    report['exact_worlds'] += int(result['exact'])
                    for i, value in result['values'].items():
                        rewards[i].append(value)
                if report['worlds'] == 0:
                    break  # Never relax public-information constraints to manufacture a deal.
                # Successive halving; retain the rule-based move as the paired reference.
                if report['worlds'] >= 16 and len(active) > 2:
                    keep = sorted(active, key=lambda i: (-sum(rewards[i]) / len(rewards[i]), i))[:max(2, (len(active)+1)//2)]
                    if baseline_index not in keep:
                        keep[-1] = baseline_index
                    eliminated.update(set(active) - set(keep))
                    active = keep
                if pending:
                    break
        finally:
            for future in pending:
                future.cancel()
        report['elapsed_seconds'] = time.monotonic() - start
        report['candidates_searched'] = len(selected)
        report['candidates'] = [{'ids': [c.id for c in action.cards], 'samples': len(rewards[i]),
                                 'mean_utility': sum(rewards[i])/len(rewards[i]) if rewards[i] else None,
                                 'eliminated': i in eliminated} for i, action in enumerate(selected)]
        if report['worlds'] == 0:
            report['reason'] = 'no_complete_comparison'
            return SearchResult(baseline, report)
        report['reason'] = 'deadline' if time.monotonic() >= deadline else 'sample_limit'
        by_face = {}
        for i, action in enumerate(selected):
            mean = sum(rewards[i]) / len(rewards[i])
            score = -1 + mean / 100 if i in eliminated else mean
            # Rule ranking is the deterministic tie-break when simulations tie.
            score -= i * 1e-9
            by_face[tuple(sorted(c.key for c in action.cards))] = (score, len(rewards[i]))
        ranked = []
        for position, item in enumerate(baseline):
            value = by_face.get(tuple(sorted(c.key for c in item.action.cards)))
            score, n = value if value is not None else (-2 - position / len(baseline), 0)
            label = f'模拟续局评分（{n} 次）' if n else '未搜索候选，按记牌策略排序'
            ranked.append(RankedAction(item.action, (Reason('search', label, score),)))
        return SearchResult(ordered(ranked), report)
