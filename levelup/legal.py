"""Exact play constraints for UI guidance, using only the player's own hand.

A follow is legal iff it has `count` cards from `pool` and contains at least
one `required` witness. This describes every legal follow without enumerating
the potentially millions of ways to fill the remaining single-card slots.
"""
from collections import Counter
from functools import lru_cache
from itertools import combinations

from .game import Rules


@lru_cache(maxsize=256)
def _constraints(level, trump, hand, lead):
    rules = Rules(level, trump)
    count = len(lead)
    suited = tuple(c for c in hand if rules.suit(c) == rules.suit(lead[0]))
    if len(suited) <= count:
        return (suited if len(suited) == count else hand), (suited,)

    demands = [p.pairs for p in rules.components(lead) if p.pairs]
    target = rules.follow_plan(suited, demands)[0]
    needed = sum(target)
    if not needed:
        return suited, ((),)
    # Every legal follow contains a witness with exactly this many pairs.
    # Extra fillers cannot improve beyond the full hand's optimal profile.
    witnesses = []
    for groups in combinations(rules.pair_groups(suited), needed):
        cards = tuple(c for group in groups for c in group)
        if max(target) <= 1 or rules.follow_plan(cards, demands)[0] == target:
            witnesses.append(cards)
    return suited, tuple(witnesses)


def _forced_play(pool, required, count):
    """Return a move only if every completion has the same multiset of faces."""
    first, signature = None, None
    for witness in required:
        fixed = set(witness)
        rest = [c for c in pool if c not in fixed]
        take = count - len(witness)
        chosen = list(witness) + rest[:take]
        faces = Counter(c.key for c in chosen)
        if signature is not None and faces != signature:
            return None
        if first is None:
            first, signature = chosen, faces
        # Any different-face swap between a filler and an unused card gives
        # a second legal play. Swapping identical deck copies does not.
        if take and len(rest) > take and len({c.key for c in rest}) > 1:
            return None
    return [c.id for c in first] if first else None


def play_options(rules, hand, lead=()):
    """Describe legal plays; identical deck copies count as the same choice.

    On lead, any nonempty same-effective-suit selection may be submitted.
    Throw success is deliberately not predicted using opponents' hidden hands.
    """
    hand, lead = tuple(hand), tuple(lead)
    if not lead:
        return {"count": None, "pool": [c.id for c in hand], "required": [],
                "forced": [hand[0].id] if len(hand) == 1 else None}
    pool, required = _constraints(rules.level, rules.trump, hand, lead)
    return {"count": len(lead), "pool": [c.id for c in pool],
            "required": [[c.id for c in witness] for witness in required],
            "forced": _forced_play(pool, required, len(lead))}
