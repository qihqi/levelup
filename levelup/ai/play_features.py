"""Public-information features shared verbatim by training and play inference."""
from collections import Counter

from .knowledge import Knowledge
from .rule_based import RULES, RuleBasedStrategy

FEATURE_VERSION = 1
RULE_NAMES = tuple(sorted(RULES))
FEATURE_NAMES = (
    'lead', 'hand_size', 'lead_size', 'trick_position', 'defending', 'attacking_score',
    'level', 'played_count', 'candidate_size', 'candidate_points', 'candidate_trump',
    'min_strength', 'mean_strength', 'max_strength', 'candidate_suit_length',
    *[f'count_relative_{i}' for i in range(4)],
    *[f'void_relative_{i}_{s}' for i in range(4) for s in 'SHCDT'],
    *[f'rule_{r}' for r in RULE_NAMES],
    *[f'hand_strength_{i}' for i in range(16)],
    *[f'trump_strength_{i}' for i in range(16)],
    *[f'candidate_strength_{i}' for i in range(16)],
    *[f'unseen_suit_strength_{i}' for i in range(16)],
)


def feature_rows(ctx, candidates):
    if ctx.phase != 'playing':
        raise ValueError('The play model accepts playing observations only')
    r, knowledge = ctx.rules, Knowledge(ctx)
    scores = {item.action.key: item for item in RuleBasedStrategy().rank(ctx, candidates)}
    strength = Counter(r.strength(c) for c in ctx.hand)
    trumps = Counter(r.strength(c) for c in ctx.hand if r.suit(c) == 'T')
    common = [float(not ctx.trick), len(ctx.hand)/25,
              len(ctx.trick[0].cards)/25 if ctx.trick else 0, len(ctx.trick)/4,
              float(ctx.defending), ctx.score/200, ctx.level/14, len(knowledge.played)/100]
    rows = []
    for action in candidates:
        cards = action.cards
        powers = [r.strength(c) for c in cards]
        suit = r.suit(cards[0])
        used = Counter(powers)
        unseen = Counter(r.strength(c) for c in knowledge.unseen if r.suit(c) == suit)
        reasons = {reason.rule: reason.value for reason in scores[action.key].reasons}
        row = [*common, len(cards)/25, sum(c.points for c in cards)/40,
               sum(r.suit(c) == 'T' for c in cards)/len(cards),
               min(powers)/16, sum(powers)/len(powers)/16, max(powers)/16,
               sum(r.suit(c) == suit for c in ctx.hand)/25,
               *[ctx.counts[(ctx.seat+i)%4]/25 for i in range(4)],
               *[float(s in knowledge.voids[(ctx.seat+i)%4]) for i in range(4) for s in 'SHCDT'],
               *[reasons.get(name, 0)/100 for name in RULE_NAMES],
               *[strength[i]/8 for i in range(16)], *[trumps[i]/8 for i in range(16)],
               *[used[i]/8 for i in range(16)], *[unseen[i]/8 for i in range(16)]]
        assert len(row) == len(FEATURE_NAMES)
        rows.append(row)
    return rows


def context_from_dict(data):
    from ..game import Card
    from .interface import AIContext, Play, Bid
    def play(p):
        return Play(p['seat'], tuple(Card(**c) for c in p['cards']))
    def bid(b):
        return Bid(b['seat'], b['value'], tuple(Card(**c) for c in b['cards']))
    return AIContext(**{**data, 'hand':tuple(Card(**c) for c in data['hand']),
        'counts':tuple(data['counts']), 'trick':tuple(play(p) for p in data['trick']),
        'history':tuple(tuple(play(p) for p in t) for t in data['history']),
        'bid':bid(data['bid']) if data['bid'] else None,
        'declarations':tuple(bid(b) for b in data['declarations']),
        'known_bottom':tuple(Card(**c) for c in data['known_bottom'])})
