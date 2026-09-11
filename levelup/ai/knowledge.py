"""Card counting and conservative possibilities, never actual opponent hands."""
from functools import cached_property, wraps
from math import prod

from ..game import deck
from .interface import AIContext


def memoized(method):
    """Per-observation cache; a global method lru_cache would retain every hand."""
    name = f"_{method.__name__}_cache"

    @wraps(method)
    def lookup(self, *args):
        cache = self.__dict__.setdefault(name, {})
        if args not in cache:
            cache[args] = method(self, *args)
        return cache[args]
    return lookup


class Knowledge:
    def __init__(self, context: AIContext):
        self.ctx = context
        self.rules = context.rules
        self.voids = {i: set() for i in range(4)}
        played = {}
        for trick in (*context.history, context.trick):
            if not trick:
                continue
            suit = self.rules.suit(trick[0].cards[0])
            for play in trick:
                played.update((c.id, c) for c in play.cards)
                if any(self.rules.suit(c) != suit for c in play.cards):
                    self.voids[play.seat].add(suit)
        self.played = tuple(played.values())
        unavailable = {c.id for c in (*context.hand, *self.played, *context.known_bottom)}
        self.unseen = tuple(c for c in deck() if c.id not in unavailable)
        exposed = {}
        for bid in context.declarations:
            exposed.update((c.id, bid.seat) for c in bid.cards if c.id not in unavailable)
        self.exposed = exposed

    @memoized
    def pool(self, seat):
        return tuple(c for c in self.unseen if self.rules.suit(c) not in self.voids[seat]
                     and self.exposed.get(c.id, seat) == seat)

    @memoized
    def suit_pool(self, seat, suit):
        return tuple(c for c in self.pool(seat) if self.rules.suit(c) == suit)

    def possible_shape(self, pool, cards, minimum=-1):
        parts = self.rules.components(cards)
        if len(parts) == 1:
            k = parts[0].pairs
            if not k:
                return any(self.rules.strength(c) > minimum for c in pool)
            pairs = self.rules.pair_groups(pool)
            return any(max(self.rules.strength(pairs[i][0]) for i in run) > minimum
                       for run in self.rules.runs(pairs, k))
        # Conservative for a composite throw: possibly beating the largest
        # component suffices to mark danger, but never to declare a safe win.
        largest = max(parts, key=lambda p: (p.pairs, p.strength))
        return self.possible_shape(pool, tuple(largest.cards), minimum)

    @memoized
    def master(self, cards):
        """No strictly larger same-suit component can remain outside this hand.

        A tied single cannot beat the leader. Two equally ranked off-suit level
        pairs are alternatives, never a tractor. Unknown bottom cards remain
        possible outside cards unless this player buried them.
        """
        suit = self.rules.suit(cards[0])
        pool = tuple(c for c in self.unseen if self.rules.suit(c) == suit)
        return all(not self.possible_shape(pool, tuple(p.cards), p.strength)
                   for p in self.rules.components(cards))

    @memoized
    def void_probability(self, seat, suit):
        if suit in self.voids[seat]:
            return 1.0
        if seat != self.ctx.dealer and any(self.exposed.get(c.id) == seat and self.rules.suit(c) == suit for c in self.unseen):
            return 0.0
        pool = self.pool(seat)
        n, length = len(pool), min(self.ctx.counts[seat], len(pool))
        suited = sum(self.rules.suit(c) == suit for c in pool)
        if n - suited < length:
            return 0.0
        # A uniform-deal estimate, not an inferred fact about a concealed hand.
        return prod((n - suited - i) / (n - i) for i in range(length)) if n else 1.0

    @memoized
    def ruff_risk(self, cards, seats):
        suit = self.rules.suit(cards[0])
        if suit == "T":
            return 0.0
        chances = []
        pairs = max(p.pairs for p in self.rules.components(cards))
        factor = 1.0 if not pairs else 0.5 if pairs == 1 else 0.25
        for seat in seats:
            if self.ctx.counts[seat] < len(cards):
                continue
            trumps = self.suit_pool(seat, "T")
            if len(trumps) >= len(cards) and self.possible_shape(trumps, cards):
                chances.append(self.void_probability(seat, suit) * factor)
        return 1 - prod(1 - chance for chance in chances)

    @memoized
    def security(self, winner_cards, lead, seats):
        """Heuristic safety of the current winner against players still to act."""
        if not seats:
            return 1.0
        suit = self.rules.suit(winner_cards[0])
        strength = self.rules.shape_strength(list(winner_cards), list(lead))
        if strength is None:
            return 0.0
        risks = []
        for seat in seats:
            if self.ctx.counts[seat] < len(lead):
                continue
            same = self.suit_pool(seat, suit)
            if self.possible_shape(same, lead, strength):
                risks.append(0.48 if suit != "T" else 0.6)
        return prod(1 - risk for risk in risks) * (1 - self.ruff_risk(winner_cards, seats))

    @cached_property
    def hand_pairs(self):
        return self.rules.pair_groups(self.ctx.hand)

    def damage(self, cards):
        """Count split pairs and split adjacent-pair links; playing a whole run is fine."""
        used = {c.id for c in cards}
        pairs = self.hand_pairs
        counts = [sum(c.id in used for c in pair) for pair in pairs]
        split_pairs = sum(n == 1 for n in counts)
        links = 0
        for i, pair in enumerate(pairs):
            for j in range(i + 1, len(pairs)):
                if (self.rules.suit(pair[0]) == self.rules.suit(pairs[j][0]) and
                        abs(self.rules.strength(pair[0]) - self.rules.strength(pairs[j][0])) == 1 and
                        counts[i] != counts[j]):
                    links += 1
        return split_pairs, links
