"""Construct plausible deals from one player's information, never a live Game."""
import random
import time

from ..game import Game, RuleError
from .knowledge import Knowledge


class DealSampler:
    """Random feasible assignments, conditioned on observed legal plays.

    Random matching plus capacity-preserving swaps is an approximate belief,
    not a claim of uniform posterior sampling or an opponent-policy model.
    """

    def __init__(self, context):
        self.ctx = context
        self.knowledge = Knowledge(context)
        self.cards = self.knowledge.unseen
        self.capacities = [n if seat != context.seat else 0
                           for seat, n in enumerate(context.counts)]
        self.capacities.append(0 if context.known_bottom else 8)
        if sum(self.capacities) != len(self.cards):
            raise ValueError('Observation does not account for all 108 cards')
        self.slots = [seat for seat, count in enumerate(self.capacities) for _ in range(count)]
        self.allowed = []
        for card in self.cards:
            exposed = self.knowledge.exposed.get(card.id)
            owners = set()
            for seat in range(5):
                if not self.capacities[seat]:
                    continue
                if exposed is not None and seat != exposed and not (seat == 4 and exposed == context.dealer):
                    continue
                if seat < 4 and context.rules.suit(card) in self.knowledge.voids[seat]:
                    continue
                owners.add(seat)
            self.allowed.append(owners)
        self.attempts = 0

    def assignment(self, rng):
        neighbors = [[j for j, owner in enumerate(self.slots) if owner in allowed]
                     for allowed in self.allowed]
        for options in neighbors:
            rng.shuffle(options)
        order = list(range(len(self.cards)))
        rng.shuffle(order)
        order.sort(key=lambda i: len(neighbors[i]))
        matched = [-1] * len(self.slots)

        def place(card, seen):
            for slot in neighbors[card]:
                if slot in seen:
                    continue
                seen.add(slot)
                if matched[slot] < 0 or place(matched[slot], seen):
                    matched[slot] = card
                    return True
            return False

        for card in order:
            if not place(card, set()):
                return None
        owners = [0] * len(self.cards)
        for slot, card in enumerate(matched):
            owners[card] = self.slots[slot]
        # Mix the initial constrained matching while preserving all capacities.
        for _ in range(8 * len(self.cards)):
            a, b = rng.randrange(len(self.cards)), rng.randrange(len(self.cards))
            if owners[b] in self.allowed[a] and owners[a] in self.allowed[b]:
                owners[a], owners[b] = owners[b], owners[a]
        holdings = [[] for _ in range(5)]
        for card, owner in zip(self.cards, owners):
            holdings[owner].append(card)
        holdings[self.ctx.seat] = list(self.ctx.hand)
        if self.ctx.known_bottom:
            holdings[4] = list(self.ctx.known_bottom)
        return holdings

    def consistent_history(self, hands):
        """Reverse plays to check obligations at the time, including pairs/tractors."""
        remaining = [list(hand) for hand in hands]
        rules = self.ctx.rules
        for trick in reversed((*self.ctx.history, self.ctx.trick)):
            if not trick:
                continue
            lead = trick[0].cards
            for index in range(len(trick) - 1, -1, -1):
                play = trick[index]
                remaining[play.seat].extend(play.cards)
                try:
                    if index:
                        rules.validate_follow(remaining[play.seat], play.cards, lead)
                    elif rules.failed_throw(list(lead), [h for s, h in enumerate(remaining) if s != play.seat]):
                        return False
                except RuleError:
                    return False
        return True

    def sample(self, rng: random.Random, deadline=float('inf'), attempts=128):
        for _ in range(attempts):
            if time.monotonic() >= deadline:
                return None
            self.attempts += 1
            holdings = self.assignment(rng)
            if holdings is not None and self.consistent_history(holdings[:4]):
                return self.game(holdings)
        return None

    def game(self, holdings):
        ctx = self.ctx
        game = Game(0)
        game.phase, game.level, game.trump = 'playing', ctx.level, ctx.trump
        game.dealer, game.turn, game.score = ctx.dealer, ctx.seat, ctx.score
        game.hands, game.bottom = holdings[:4], holdings[4]
        game.levels = [ctx.level, ctx.level]
        game.history = [{'plays': Game.serialize_trick([(p.seat, p.cards) for p in trick])}
                        for trick in ctx.history]
        game.trick = [(p.seat, list(p.cards)) for p in ctx.trick]
        game.declarations = [{'seat': b.seat, 'value': b.value, 'cards': [c.json() for c in b.cards]}
                             for b in ctx.declarations]
        game.bid = None if ctx.bid is None else {
            'seat': ctx.bid.seat, 'value': ctx.bid.value, 'cards': [c.json() for c in ctx.bid.cards]}
        return game
