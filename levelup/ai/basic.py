"""The original heuristic, kept as a selectable baseline for comparisons."""
from collections import Counter, defaultdict

from ..game import RuleError, bid_value
from .interface import AIStrategy, Action, RankedAction, Reason, ordered


def winning_response(ctx, pool, lead):
    rules = ctx.rules
    parts = rules.components(lead)
    if len(parts) != 1:
        return None
    if parts[0].pairs:
        pairs = rules.pair_groups(pool)
        candidates = [[c for i in run for c in pairs[i]] for run in rules.runs(pairs, parts[0].pairs)]
    else:
        candidates = [[c] for c in pool]
    winners = [cs for cs in candidates if rules.winner(ctx.table + [(ctx.seat, cs)]) == ctx.seat]
    return min(winners, key=lambda cs: max(rules.strength(c) for c in cs)) if winners else None


def baseline_action(ctx):
    hand, rules, seat = ctx.hand, ctx.rules, ctx.seat
    if ctx.phase == "dealing":
        groups = defaultdict(list)
        for c in hand:
            groups[c.key].append(c)
        options = []
        for group in groups.values():
            try:
                value, trump = bid_value(group, ctx.level)
            except RuleError:
                continue
            if ctx.bid and (value <= ctx.bid.value or
                (ctx.bid.seat == seat and not (trump == ctx.trump and value == 2))):
                continue
            options.append((sum(c.suit == trump for c in hand), value, group))
        return Action("bid", tuple(max(options, key=lambda o: o[:2])[2])) if options else Action("pass")
    if ctx.phase == "burying":
        counts = Counter(c.key for c in hand)
        chosen = sorted(hand, key=lambda c: (rules.suit(c) == "T", c.points > 0,
                                           counts[c.key] == 2, rules.strength(c)))[:8]
        return Action("bury", tuple(chosen))
    low = lambda c: (rules.suit(c) == "T", c.points, rules.strength(c))
    if not ctx.trick:
        candidates = []
        for suit in "SHCDT":
            suited = [c for c in hand if rules.suit(c) == suit]
            if suited:
                candidates.extend(p.cards for p in rules.components(suited))
        chosen = min(candidates, key=lambda cs: (rules.suit(cs[0]) == "T", -len(cs),
                                                sum(c.points for c in cs), rules.strength(cs[0])))
    else:
        lead = list(ctx.trick[0].cards)
        n, suit = len(lead), rules.suit(lead[0])
        suited = [c for c in hand if rules.suit(c) == suit]
        partner_winning = rules.winner(ctx.table) % 2 == seat % 2
        if len(suited) >= n:
            demands = [p.pairs for p in rules.components(lead) if p.pairs]
            _, chosen = rules.follow_plan(suited, demands)
            remaining = [c for c in suited if c not in chosen]
            remaining.sort(key=lambda c: (-c.points if partner_winning else c.points, rules.strength(c)))
            chosen += remaining[:n - len(chosen)]
            if not partner_winning:
                chosen = winning_response(ctx, suited, lead) or chosen
        else:
            chosen = list(suited)
            remaining = [c for c in hand if c not in chosen]
            trumps = [c for c in remaining if rules.suit(c) == "T"]
            if not suited and not partner_winning and len(trumps) >= n:
                demands = [p.pairs for p in rules.components(lead) if p.pairs]
                _, attack = rules.follow_plan(trumps, demands)
                attack += sorted([c for c in trumps if c not in attack], key=low)[:n - len(attack)]
                attack = winning_response(ctx, trumps, lead) or attack
                if rules.shape_strength(attack, lead) is not None and rules.winner(ctx.table + [(seat, attack)]) == seat:
                    chosen = attack
            if len(chosen) < n:
                remaining.sort(key=(lambda c: (-c.points, rules.suit(c) == "T", rules.strength(c))) if partner_winning else low)
                chosen += remaining[:n - len(chosen)]
    return Action("play", tuple(chosen))


class BasicStrategy(AIStrategy):
    id = "basic"
    name = "基础 AI"
    description = "原版策略：优先副牌长组合，能压则用最小牌压，队友领先时送分。"

    def rank(self, context, candidates):
        preferred = baseline_action(context).key
        return ordered(RankedAction(action, (Reason("legacy", "原版启发式优先选择",
                                                    1.0 if action.key == preferred else 0.0),))
                       for action in candidates)
