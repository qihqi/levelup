"""Full-information teacher: exhaustive small endgames and rollout estimates."""
from copy import copy
from itertools import combinations

from .game import Card, Game, RuleError
from .legal import play_options
from .ai.interface import AIContext, Play
from .ai.candidates import generate_candidates, simple_combinations
from .ai.interface import Action
from .ai.knowledge import Knowledge
from .ai.rule_based import RuleBasedStrategy


def context(game):
    return AIContext(seat=game.turn, phase='playing', hand=tuple(game.hands[game.turn]),
                     level=game.level, trump=game.trump, dealer=game.dealer, score=game.score,
                     counts=tuple(map(len, game.hands)),
                     trick=tuple(Play(s, tuple(cs)) for s, cs in game.trick),
                     known_bottom=tuple(game.bottom))


def restore(sample):
    o, p = sample['observation'], sample['privileged']
    game = Game(0)
    game.phase, game.level, game.trump = 'playing', o['level'], o['trump']
    game.dealer, game.turn, game.score = o['dealer'], o['seat'], o['score']
    game.hands = [[Card(**c) for c in hand] for hand in p['hands']]
    game.bottom = [Card(**c) for c in p['bottom']]
    game.trick = [(t['seat'], [Card(**c) for c in t['cards']]) for t in o['trick']]
    game.levels = [game.level, game.level]
    return game


def fork(game):
    child = copy(game)
    child.hands = [list(hand) for hand in game.hands]
    child.trick = list(game.trick)
    child.history = list(game.history)
    child.events = list(game.events)
    child.levels = list(game.levels)
    return child


def face_key(cards):
    return tuple(sorted(c.key for c in cards))


def exhaustive_actions(game):
    """All distinct face-multiset submissions; only called on tiny endgames."""
    hand, r = game.hands[game.turn], game.rules
    seen = set()
    sizes = [len(game.trick[0][1])] if game.trick else range(1, len(hand)+1)
    for size in sizes:
        for cs in combinations(hand, size):
            key = face_key(cs)
            if key in seen:
                continue
            seen.add(key)
            try:
                if game.trick:
                    r.validate_follow(hand, cs, game.trick[0][1])
                else:
                    r.components(cs)
            except RuleError:
                continue
            yield Action('play', tuple(cs))


def state_key(game):
    return (game.turn, tuple(tuple(sorted(c.id for c in h)) for h in game.hands),
            tuple((s, tuple(sorted(c.id for c in cs))) for s, cs in game.trick), game.score)


class SearchLimit(Exception):
    pass


class ExactSolver:
    def __init__(self, node_limit=30000):
        self.cache, self.nodes, self.node_limit = {}, 0, node_limit

    def value(self, game):
        if game.phase in ('round_end', 'match_end'):
            return game.score
        key = state_key(game)
        if key in self.cache:
            return self.cache[key]
        self.nodes += 1
        if self.nodes > self.node_limit:
            raise SearchLimit()
        values = []
        for action in exhaustive_actions(game):
            child = fork(game)
            child.act(child.turn, 'play', [c.id for c in action.cards])
            values.append(self.value(child))
        value = min(values) if game.turn % 2 == game.dealer % 2 else max(values)
        self.cache[key] = value
        return value


class GodKnowledge(Knowledge):
    """Teacher-only override: actual live holdings replace beliefs."""
    def __init__(self, ctx, game):
        self.ctx, self.rules, self.game = ctx, ctx.rules, game
        self.voids = {s: set('SHCDT') - {self.rules.suit(c) for c in game.hands[s]} for s in range(4)}
        self.unseen = tuple(c for s, hand in enumerate(game.hands) if s != ctx.seat for c in hand)
        self.played, self.exposed = (), {}

    def pool(self, seat):
        return tuple(self.game.hands[seat])

    def void_probability(self, seat, suit):
        return float(suit in self.voids[seat])

    def ruff_risk(self, cards, seats):
        suit = self.rules.suit(cards[0])
        if suit == 'T':
            return 0.0
        return float(any(suit in self.voids[s] and
                         self.possible_shape(self.suit_pool(s, 'T'), cards) for s in seats))

    def security(self, winner_cards, lead, seats):
        r, suit = self.rules, self.rules.suit(winner_cards[0])
        strength = r.shape_strength(winner_cards, lead)
        if strength is None:
            return 0.0
        for seat in seats:
            if self.possible_shape(self.suit_pool(seat, suit), lead, strength):
                return 0.0
        return 1 - self.ruff_risk(winner_cards, seats)


POLICIES = (RuleBasedStrategy(), RuleBasedStrategy({'spend_strength':-1.2, 'win_control':40,
                                                 'lead_trump':-10, 'feed_partner':2.0}))


def rollout_action(game, variant):
    ctx = context(game)
    k, policy = GodKnowledge(ctx, game), POLICIES[variant % len(POLICIES)]
    if ctx.trick:
        candidates = generate_candidates(ctx)
    else:
        candidates = list(Action('play', cs) for cs in simple_combinations(ctx.rules, ctx.hand))
        for suit in 'SHCDT':
            cs = tuple(c for c in ctx.hand if ctx.rules.suit(c) == suit)
            if len(cs) > 1 and ctx.rules.failed_throw(cs, [h for i,h in enumerate(game.hands) if i!=ctx.seat]) is None:
                candidates.append(Action('play', cs))
    best, best_score = None, float('-inf')
    for action in candidates:
        score = 0.0
        def add(rule, feature=1.0):
            nonlocal score
            score += feature * policy.weights[rule]
        if ctx.trick:
            policy.score_follow(ctx, action.cards, k, add)
        else:
            policy.score_lead(ctx, action.cards, k, add)
        if score > best_score or (score == best_score and (best is None or action.key < best.key)):
            best, best_score = action, score
    return best


def rollout(game, variant, memo):
    path = []
    for _ in range(110):
        if game.phase in ('round_end', 'match_end'):
            value = game.score
            break
        key = (state_key(game), variant)
        if key in memo:
            value = memo[key]
            break
        path.append(key)
        action = rollout_action(game, variant)
        game.act(game.turn, 'play', [c.id for c in action.cards])
    else:
        raise RuntimeError('Teacher rollout did not terminate')
    for key in path:
        memo[key] = value
    return value


def label(sample, rollouts=2):
    game = restore(sample)
    forced = play_options(game.rules, game.hands[game.turn], game.trick[0][1] if game.trick else ())['forced']
    if forced is not None:
        return {'method':'forced', 'candidate_values':[None]*len(sample['candidates']),
                'best_candidate_indices':list(range(len(sample['candidates']))),
                'best_play':sample['candidates'][0], 'proven_optimal':True,
                'global_best_value':None, 'rollouts_per_candidate':0, 'exact_nodes':0}
    direction = -1 if game.turn % 2 == game.dealer % 2 else 1
    values, method, global_best = [], 'full_information_rollouts', None
    solver = ExactSolver()
    if max(map(len, game.hands)) <= 3:
        try:
            global_best = direction * solver.value(game)
            for candidate in sample['candidates']:
                child = fork(game)
                child.act(child.turn, 'play', candidate['ids'])
                values.append(direction * solver.value(child))
            method = 'exhaustive_minimax'
        except SearchLimit:
            values, global_best = [], None
    if not values:
        # Identical face choices (including failed throws) may share a successor.
        cache, memo = {}, {}
        for candidate in sample['candidates']:
            child = fork(game)
            child.act(child.turn, 'play', candidate['ids'])
            key = state_key(child)
            if key not in cache:
                cache[key] = direction * sum(rollout(fork(child), i, memo) for i in range(rollouts)) / rollouts
            values.append(cache[key])
    best = max(values)
    indices = [i for i,v in enumerate(values) if v == best]
    return {'method':method, 'objective':'maximize team-oriented final attacking score',
            'candidate_values':values, 'best_candidate_indices':indices,
            'best_play':sample['candidates'][indices[0]],
            'proven_optimal': global_best is not None and best == global_best,
            'global_best_value':global_best, 'root_candidates_exhaustive':False,
            'rollouts_per_candidate':rollouts if method != 'exhaustive_minimax' else 0,
            'exact_nodes':solver.nodes}
