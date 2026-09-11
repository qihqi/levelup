"""Compare the UI's compact constraints with exhaustive authoritative legality."""
from collections import Counter
from itertools import combinations
import random

import pytest

from levelup.game import Card, RuleError, Rules, deck
from levelup.legal import play_options


def cards(*faces):
    return [Card(i, suit, rank) for i, (suit, rank) in enumerate(faces)]


def assert_exact(rules, hand, lead):
    options = play_options(rules, hand, lead)
    pool = set(options['pool'])
    required = [set(ids) for ids in options['required']]
    legal = []
    for move in combinations(hand, len(lead)):
        ids = {c.id for c in move}
        described = ids <= pool and any(witness <= ids for witness in required)
        try:
            rules.validate_follow(hand, move, lead)
            valid = True
        except RuleError:
            valid = False
        assert described == valid, (hand, lead, move, options)
        if valid:
            legal.append(move)
    assert legal
    faces = {tuple(sorted(Counter(c.key for c in move).items())) for move in legal}
    assert (options['forced'] is not None) == (len(faces) == 1)
    if options['forced'] is not None:
        assert set(options['forced']) in [{c.id for c in move} for move in legal]
    return options


@pytest.mark.parametrize('trump,level,hand,lead', [
    ('S', 2, [('H', 3), ('H', 3), ('H', 5), ('C', 4)], [('H', 7)] * 2),
    ('S', 2, [('H', 3), ('H', 3), ('H', 4), ('H', 4), ('H', 6), ('H', 6), ('H', 9)],
     [('H', 7)] * 2 + [('H', 8)] * 2),
    # Broken tractors, multiple demands, and extra loose-card fillers.
    ('S', 2, [(s, r) for s, r in [('H', 3), ('H', 4), ('H', 6), ('H', 8), ('H', 9)] for _ in range(2)],
     [('H', 10)] * 2 + [('H', 11)] * 2 + [('H', 13)] * 2),
    ('S', 2, [('H', 3), ('H', 3), ('H', 4), ('H', 4), ('H', 8), ('H', 10)],
     [('H', 6)] * 2 + [('H', 9)]),
    # A short suit is mandatory even when there are pairs in another suit.
    ('S', 2, [('H', 3), ('C', 4), ('C', 4), ('D', 8)], [('H', 7)] * 2),
    ('S', 2, [('C', 4), ('C', 4), ('D', 8)], [('H', 7)] * 2),
    # Level cards are trumps; equal-strength off-suit level pairs are alternatives.
    ('S', 2, [('H', 2), ('H', 2), ('C', 2), ('C', 2), ('S', 2), ('S', 2), ('J', 15), ('J', 15)],
     [('S', 13)] * 2 + [('S', 14)] * 2),
    (None, 14, [('H', 14), ('H', 14), ('C', 14), ('C', 14), ('J', 15), ('J', 15)],
     [('D', 14)] * 2 + [('J', 15)] * 2),
    ('S', 2, [('H', 9), ('H', 9), ('C', 8)], [('H', 7)]),
])
def test_exact_constraints_for_structural_and_trump_cases(trump, level, hand, lead):
    assert_exact(Rules(level, trump), cards(*hand), cards(*lead))


def test_randomized_constraints_and_forced_moves_against_all_subsets():
    rng = random.Random(4096)
    for _ in range(120):
        rules = Rules(rng.choice([2, 7, 14]), rng.choice([None, 'S', 'H']))
        # Dense suit samples exercise pairs and runs much more than full decks.
        population = [c for c in deck() if c.suit in ('H', 'S') and c.rank <= 9]
        hand = rng.sample(population, rng.randint(5, 11))
        # Use a single effective suit for each generated lead.
        suit = rules.suit(rng.choice(population))
        lead_pool = [c for c in population if rules.suit(c) == suit]
        lead = rng.sample(lead_pool, rng.randint(1, min(6, len(lead_pool), len(hand))))
        assert_exact(rules, hand, lead)


def test_lead_does_not_auto_choose_a_pair_over_a_single_or_predict_throw_success():
    rules = Rules()
    hand = cards(('H', 9), ('H', 9))
    assert play_options(rules, hand)['forced'] is None
    assert play_options(rules, hand[:1])['forced'] == [0]
    assert play_options(rules, hand)['pool'] == [0, 1]


def test_large_discard_space_is_compact_and_not_mistaken_for_a_forced_move():
    hand = [c for c in deck() if c.suit != 'H' and c.rank != 2][:25]
    options = play_options(Rules(2, 'S'), hand, cards(*([('H', 8)] * 12)))
    assert options['required'] == [[]]
    assert options['pool'] == [c.id for c in hand]
    assert options['forced'] is None
