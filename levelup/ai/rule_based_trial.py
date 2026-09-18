"""Experimental play-only fork; deliberately absent from the live registry.

Uses the same actions, bidding and burying as the incumbent. Estimates use only
the immutable player observation, never the real concealed hands.
"""
from math import prod

from .interface import RankedAction, Reason, ordered
from .knowledge import Knowledge, memoized
from .rule_based import RULES, RuleBasedStrategy


def avoiding(total, targets, draws):
    """Hypergeometric probability of drawing none of the target cards."""
    draws = min(total, max(0, draws))
    if total - targets < draws:
        return 0.0
    return prod((total - targets - i) / (total - i) for i in range(draws))


class CountingKnowledge(Knowledge):
    def __init__(self, context):
        super().__init__(context)
        self.no_pairs = {seat: set() for seat in range(4)}
        for trick in (*context.history, context.trick):
            if not trick:
                continue
            lead = trick[0].cards
            if not self.rules.pair_groups(lead):
                continue
            suit = self.rules.suit(lead[0])
            for play in trick[1:]:
                suited = [c for c in play.cards if self.rules.suit(c) == suit]
                if not self.rules.pair_groups(suited):
                    self.no_pairs[play.seat].add(suit)

    @memoized
    def holding(self, seat, required, forbidden_suit=None):
        """Uniform hand estimate for a required set and optional suit void.

        Public exposed cards of non-dealers are fixed. A dealer may have buried
        an exposed card, so its location is uncertain. This is an estimate, not
        a calibrated posterior over strategic play/burying.
        """
        pool = self.pool(seat)
        ids = {c.id for c in pool}
        required = set(required)
        if not required <= ids:
            return 0.0
        fixed = {c.id for c in pool if self.exposed.get(c.id) == seat}
        if seat == self.ctx.dealer:
            fixed = set()
        forbidden = {c.id for c in pool if self.rules.suit(c) == forbidden_suit}
        if (required | fixed) & forbidden:
            return 0.0
        n, take = len(pool) - len(fixed), self.ctx.counts[seat] - len(fixed)
        need = len(required - fixed)
        if not 0 <= need <= take <= n:
            return 0.0
        includes = prod((take - i) / (n - i) for i in range(need))
        return includes * avoiding(n - need, len(forbidden), take - need)

    @memoized
    def beating(self, seat, cards, suit, minimum, forbidden_suit=None):
        """Chance of a higher matching single/pair/tractor, conditional on void."""
        pool = self.suit_pool(seat, suit)
        parts = self.rules.components(cards)
        part = max(parts, key=lambda p: (p.pairs, p.strength))
        if not part.pairs:
            # Exact marginal for at least one higher single and no forbidden suit.
            all_cards = self.pool(seat)
            fixed = [c for c in all_cards if self.exposed.get(c.id) == seat
                     and seat != self.ctx.dealer]
            fixed_ids = {c.id for c in fixed}
            if any(self.rules.suit(c) == forbidden_suit for c in fixed):
                return 0.0
            n = len(all_cards) - len(fixed)
            take = min(n, max(0, self.ctx.counts[seat] - len(fixed)))
            forbidden = sum(self.rules.suit(c) == forbidden_suit for c in all_cards)
            void = avoiding(n, forbidden, take)
            high = [c for c in pool if self.rules.strength(c) > minimum]
            if any(c.id in fixed_ids for c in high):
                return void
            return max(0.0, void - avoiding(n, forbidden + len(high), take))
        if suit in self.no_pairs[seat]:
            return 0.0
        pairs = self.rules.pair_groups(pool)
        chances = []
        for run in self.rules.runs(pairs, part.pairs):
            if max(self.rules.strength(pairs[i][0]) for i in run) > minimum:
                ids = tuple(c.id for i in run for c in pairs[i])
                chances.append(self.holding(seat, ids, forbidden_suit))
        # Runs can overlap: this independence approximation is intentionally
        # bounded and heuristic. Composite throws retain the largest component.
        return 1 - prod(1 - p for p in chances)

    @memoized
    def ruff_risk(self, cards, seats):
        suit = self.rules.suit(cards[0])
        if suit == "T":
            return 0.0
        return 1 - prod(1 - self.beating(seat, cards, "T", -1, suit)
                        for seat in seats if self.ctx.counts[seat] >= len(cards))

    @memoized
    def security(self, winner_cards, lead, seats):
        suit, led = self.rules.suit(winner_cards[0]), self.rules.suit(lead[0])
        strength = self.rules.shape_strength(list(winner_cards), list(lead))
        if strength is None:
            return 0.0
        risks = []
        for seat in seats:
            if self.ctx.counts[seat] < len(lead):
                continue
            if suit == "T":
                # A higher trump can overruff only if its owner is void in lead.
                risk = self.beating(seat, lead, "T", strength, led if led != "T" else None)
            else:
                risk = self.beating(seat, lead, suit, strength)
                risk += self.beating(seat, lead, "T", -1, led)
            risks.append(min(1.0, risk))
        return prod(1 - p for p in risks)

    @memoized
    def response_above(self, seat, lead, strength):
        """Higher legal response in a common ordering: side < trump."""
        suit = self.rules.suit(lead[0])
        if suit == "T":
            return self.beating(seat, lead, "T", strength)
        if strength >= 100:
            return self.beating(seat, lead, "T", strength - 100, suit)
        return min(1.0, self.beating(seat, lead, suit, strength)
                   + self.beating(seat, lead, "T", -1, suit))

    @memoized
    def partner_rescue(self, winner_cards, lead, winner_is_team):
        """Extra team-winning chance from a partner who has not played yet.

        Integrate possible highest legal partner responses against later enemy
        replies. Equal ranks respect actual play order. Independent opponent
        hands remain a heuristic approximation; this is not hidden-hand search.
        """
        ctx, r = self.ctx, self.rules
        if ctx.partner not in ctx.remaining_seats:
            return 0.0
        base = r.shape_strength(list(winner_cards), list(lead))
        if base is None:
            return 0.0
        led = r.suit(lead[0])
        if led != "T" and r.suit(winner_cards[0]) == "T":
            base += 100
        enemies = [s for s in ctx.remaining_seats if s in ctx.enemies]
        current_safety = prod(1 - self.response_above(s, lead, base) for s in enemies)
        strengths = range(16) if led == "T" else (*range(12), *range(100, 116))
        chance = 0.0
        for strength in strengths:
            if strength <= base:
                continue
            mass = (self.response_above(ctx.partner, lead, strength - 1)
                    - self.response_above(ctx.partner, lead, strength))
            if mass <= 0:
                continue
            holds = prod(1 - self.response_above(s, lead, strength - int(
                ctx.remaining_seats.index(s) < ctx.remaining_seats.index(ctx.partner)))
                         for s in enemies)
            chance += mass * (max(0, holds - current_safety) if winner_is_team else holds)
        return min(1.0, max(0.0, chance))


EXTRA_RULES = {
    "likely_lead": ("按剩余大牌估计领出成功率", 48.0),
    "save_entry": ("保留副牌进手张", -24.0),
    "team_only_trumps": ("敌方无主时保留我方主牌", -75.0),
    "partner_void_pair": ("避免对子挡住队友单主毙牌", -20.0),
    "partner_rescue": ("计入后手队友接牌与回收分数的机会", 1.0),
    "entry_to_winners": ("夺回牌权兑现手中副牌大牌", 16.0),
}


class TrialRuleBasedStrategy(RuleBasedStrategy):
    id = "rule_based_trial"
    name = "实验记牌 AI"
    description = "实验版：剩余牌概率、对子缺失推断与进手张保护。"

    def __init__(self, *, variant="partnership"):
        super().__init__()
        if variant not in ("full", "counting", "tactics", "partnership", "initiative"):
            raise ValueError("Unknown trial variant")
        self.variant = variant

    def rank(self, context, candidates):
        if context.phase != "playing":
            return super().rank(context, candidates)
        knowledge = (Knowledge if self.variant == "tactics" else CountingKnowledge)(context)
        rankings = []
        for action in candidates:
            reasons = []

            def add(rule, feature=1.0):
                if self.variant == "counting" and rule in EXTRA_RULES:
                    return
                label, default = (EXTRA_RULES if rule in EXTRA_RULES else RULES)[rule]
                value = feature * (default if rule in EXTRA_RULES else self.weights[rule])
                if self.variant == "initiative" and context.phase == "playing":
                    if rule in ("break_pair", "break_tractor"):
                        value *= min(1.0, len(context.hand) / 12)
                if abs(value) > 1e-9:
                    reasons.append(Reason(rule, label, value))

            cards = action.cards
            r = context.rules
            if not context.trick:
                self.score_lead(context, cards, knowledge, add)
                if not knowledge.master(cards):
                    add("likely_lead", knowledge.security(cards, cards, context.enemies))
                if r.suit(cards[0]) == "T" and all(
                    not knowledge.suit_pool(s, "T") for s in context.enemies
                ) and len(cards) < len(context.hand):
                    add("team_only_trumps")
                elif len(cards) > 1 and r.suit(cards[0]) in knowledge.voids[context.partner]:
                    add("partner_void_pair")
            else:
                self.score_follow(context, cards, knowledge, add)
                winner = r.winner(context.table + [(context.seat, list(cards))])
                if self.variant in ("partnership", "initiative"):
                    winner_cards = cards if winner == context.seat else next(
                        p.cards for p in context.trick if p.seat == winner)
                    rescue = knowledge.partner_rescue(winner_cards, context.trick[0].cards,
                                                      winner % 2 == context.seat % 2)
                    own_points = sum(c.points for c in cards)
                    points = own_points + sum(c.points for p in context.trick for c in p.cards)
                    value = self.weights["win_control"] + self.weights["capture_points"] * points
                    value -= self.weights["lose_points"] * own_points
                    if len(cards) == len(context.hand):
                        bottom = sum(c.points for c in context.known_bottom) if context.known_bottom else 15
                        value += 2 * (self.weights["last_trick"] + self.weights["bottom_value"]
                                      * bottom * r.bottom_multiplier(list(context.trick[0].cards)))
                    add("partner_rescue", rescue * value)
                if (self.variant == "initiative" and winner == context.seat
                        and r.winner(context.table) != context.partner):
                    remaining = tuple(c for c in context.hand if c not in cards)
                    cashable = sum(1 - knowledge.ruff_risk((c,), context.enemies)
                                   for c in remaining if r.suit(c) != "T" and knowledge.master((c,)))
                    confidence = knowledge.security(cards, context.trick[0].cards,
                                                     tuple(s for s in context.remaining_seats if s in context.enemies))
                    add("entry_to_winners", min(3, cashable) * confidence)
                if len(cards) < len(context.hand) and winner != context.seat:
                    add("save_entry", sum(r.suit(c) != "T" and knowledge.master((c,))
                                          for c in cards))
            rankings.append(RankedAction(action, tuple(reasons)))
        return ordered(rankings)
