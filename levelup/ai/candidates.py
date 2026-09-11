"""Bounded legal candidate generation, shared by all ranking strategies.

Simple leads and exact matching responses are enumerated. Huge mixed-card
spaces use structural witnesses, diverse fillers and local substitutions.
No strategy may turn legality or access to hidden hands into a scoring feature.
"""
from collections import Counter, defaultdict
from itertools import combinations
from math import comb

from ..game import RuleError, bid_value
from .basic import baseline_action
from .interface import Action
from .knowledge import Knowledge

EXACT_LIMIT = 384
LOCAL_LIMIT = 64


def simple_combinations(rules, cards):
    """One physical representative of each single, each pair, and every tractor."""
    faces = {}
    for c in cards:
        faces.setdefault(c.key, c)
    yield from ((c,) for c in faces.values())
    for suit in "SHCDT":
        pairs = rules.pair_groups([c for c in cards if rules.suit(c) == suit])
        for length in range(1, len(pairs) + 1):
            for run in rules.runs(pairs, length):
                yield tuple(c for i in run for c in pairs[i])


def sort_orders(ctx):
    r = ctx.rules
    counts = Counter(c.key for c in ctx.hand)
    return (
        lambda c: (r.suit(c) == "T", c.points, r.strength(c), c.id),
        lambda c: (-c.points, r.suit(c) == "T", r.strength(c), c.id),
        lambda c: (r.strength(c), c.points, c.id),
        lambda c: (-r.strength(c), -c.points, c.id),
        lambda c: (r.suit(c) == "T", c.rank == 14, counts[c.key] == 2, c.points, r.strength(c), c.id),
        lambda c: (r.suit(c) == "T", counts[c.key] == 2, c.points, r.strength(c), c.id),
    )


def generate_candidates(ctx):
    fallback = baseline_action(ctx)
    actions = {fallback.key: fallback}
    r, hand = ctx.rules, ctx.hand

    def add(kind, cards=()):
        action = Action(kind, tuple(sorted(cards, key=lambda c: c.id)))
        actions.setdefault(action.key, action)

    if ctx.phase == "dealing":
        add("pass")
        groups = defaultdict(list)
        for c in hand:
            groups[c.key].append(c)
        for group in groups.values():
            for length in range(1, len(group) + 1):
                cs = group[:length]
                try:
                    value, trump = bid_value(cs, ctx.level)
                except RuleError:
                    continue
                if ctx.bid and (value <= ctx.bid.value or
                    (ctx.bid.seat == ctx.seat and not (trump == ctx.trump and value == 2))):
                    continue
                add("bid", cs)
    elif ctx.phase == "burying":
        orders = sort_orders(ctx)
        suits = [s for s in "SHCD" if any(r.suit(c) == s for c in hand)]
        for count in range(len(suits) + 1):
            for cut in combinations(suits, count):
                forced = [c for c in hand if r.suit(c) in cut]
                if len(forced) > 8:
                    continue
                rest = [c for c in hand if c not in forced]
                for key in orders:
                    add("bury", forced + sorted(rest, key=key)[:8 - len(forced)])
        # Small local alternatives to the safe baseline; whole-suit cuts above
        # cover large, intentional changes that one-card swaps cannot reach.
        for old in fallback.cards:
            for new in hand:
                if new not in fallback.cards:
                    add("bury", [c for c in fallback.cards if c != old] + [new])
    elif not ctx.trick:
        for cs in simple_combinations(r, hand):
            add("play", cs)
        knowledge = Knowledge(ctx)
        for suit in "SHCDT":
            suited = tuple(c for c in hand if r.suit(c) == suit)
            if len(suited) > 1 and len(r.components(suited)) > 1 and knowledge.master(suited):
                add("play", suited)  # Only provably legal same-suit throws.
    else:
        lead = ctx.trick[0].cards
        n, suit = len(lead), r.suit(lead[0])
        suited = tuple(c for c in hand if r.suit(c) == suit)
        enough = len(suited) >= n
        fixed = () if enough else suited
        pool = suited if enough else tuple(c for c in hand if c not in fixed)
        take = n - len(fixed)
        demands = [p.pairs for p in r.components(lead) if p.pairs]
        target = r.follow_plan(suited, demands)[0] if enough else None

        def offer(cs):
            cs = tuple(cs)
            if len(cs) != n or len({c.id for c in cs}) != n:
                return
            if sum(r.suit(c) == suit for c in cs) != min(n, len(suited)):
                return
            if enough and r.follow_plan(cs, demands)[0] != target:
                return
            add("play", cs)

        for cs in simple_combinations(r, pool):
            if len(cs) == take:
                offer(fixed + cs)
        if comb(len(pool), take) <= EXACT_LIMIT:
            for cs in combinations(pool, take):
                offer(fixed + cs)
        else:
            for key in sort_orders(ctx):
                offer(fixed + tuple(sorted(pool, key=key)[:take]))
                if enough:
                    # Excluding each face explores alternate legal structural
                    # witnesses without enumerating millions of card subsets.
                    for face in {None, *(c.key for c in pool)}:
                        reduced = [c for c in pool if c.key != face]
                        profile, chosen = r.follow_plan(reduced, demands)
                        if profile == target:
                            rest = sorted([c for c in pool if c not in chosen], key=key)
                            offer(tuple(chosen + rest[:n - len(chosen)]))
            offered = 0
            for old in fallback.cards:
                if old in fixed:
                    continue
                for new in pool:
                    if new not in fallback.cards:
                        offer(tuple(c for c in fallback.cards if c != old) + (new,))
                        offered += 1
                        if offered >= LOCAL_LIMIT:
                            break
                if offered >= LOCAL_LIMIT:
                    break
    return tuple(actions.values())
