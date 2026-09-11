"""Explainable weighted rules. Sources, adaptations and caveats: docs/AI_STRATEGIES.md."""
from collections import Counter

from ..game import Rules, bid_value
from .interface import AIStrategy, RankedAction, Reason, ordered
from .knowledge import Knowledge

# (human-readable reason, weight). Features are multiplied by these weights.
# These are hand-tuned heuristic utilities, not calibrated win probabilities.
RULES = {
    "bid_length": ("主牌长度", 2.0),
    "bid_pairs": ("主牌对子", 3.0),
    "bid_controls": ("级牌与王的控制力", 2.5),
    "bid_threshold": ("亮主需有足够牌力", -20.0),
    "respect_partner_bid": ("避免轻易反队友的主", -28.0),
    "bid_improvement": ("改主后的牌力提升", 1.5),
    "reinforce": ("同花色加固", 4.0),
    "bury_trump": ("扣底保留主牌", -24.0),
    "bury_master": ("扣底保留副牌大牌", -28.0),
    "bury_points": ("控制底牌分数风险", -1.1),
    "bury_pair": ("扣底保留完整对子", -5.0),
    "make_void": ("扣短门制造缺门", 27.0),
    "concentrate": ("扣底后副牌集中", 0.13),
    "break_pair": ("避免拆开对子", -12.0),
    "break_tractor": ("避免拆开拖拉机", -16.0),
    "cash_master": ("兑现已知同门最大组合", 80.0),
    "cash_ace": ("优先兑现副牌 A", 18.0),
    "short_master": ("先兑现短门大牌", 10.0),
    "ruff_risk": ("敌家缺门毙牌风险", -145.0),
    "lead_combo": ("成套出牌减少散牌", 5.0),
    "lead_strength": ("领出牌的控制力", 1.5),
    "safe_lead_points": ("用大牌安全带走分牌", 0.7),
    "unsafe_lead_points": ("避免主动送出不安全的分牌", -2.5),
    "lead_trump": ("避免无目的调主", -24.0),
    "draw_trump": ("主强时调主保护副牌", 64.0),
    "partner_ruff": ("出单张给缺门队友毙牌", 28.0),
    "win_control": ("取得有希望保住的牌权", 24.0),
    "capture_points": ("争取本墩分数", 2.6),
    "lose_points": ("避免给敌方送分", -2.8),
    "feed_partner": ("队友安全领先时送分", 1.2),
    "overtake_partner": ("避免无益地压过队友", -24.0),
    "spend_strength": ("用足够小的牌完成任务", -0.6),
    "spend_trump": ("节省主牌", -4.5),
    "spend_joker": ("节省王的控制力", -5.0),
    "empty_ruff": ("无分墩少浪费主牌", -24.0),
    "point_threshold": ("争取或守住升级分数线", 24.0),
    "last_trick": ("争夺末墩与底牌", 80.0),
    "bottom_value": ("末墩底牌收益或风险", 1.5),
    "keep_last_trump": ("残局留最后一张主保底或抠底", -23.0),
    "shed_suit": ("垫光一门副牌", 5.0),
}


class RuleBasedStrategy(AIStrategy):
    id = "rule_based"
    name = "记牌策略 AI"
    description = "记牌找大牌、判断缺门，按控牌、配合、分数与底牌风险为候选出牌打分。"

    def __init__(self, weights=None):
        unknown = set(weights or {}) - RULES.keys()
        if unknown:
            raise ValueError(f"Unknown AI rule weights: {sorted(unknown)}")
        self.weights = {key: value[1] for key, value in RULES.items()} | (weights or {})

    def rank(self, context, candidates):
        knowledge = Knowledge(context)
        rankings = []
        for action in candidates:
            reasons = []

            def add(rule, feature=1.0):
                value = feature * self.weights[rule]
                if abs(value) > 1e-9:
                    reasons.append(Reason(rule, RULES[rule][0], value))

            if context.phase == "dealing":
                self.score_bid(context, action, add)
            elif context.phase == "burying":
                self.score_bury(context, action.cards, knowledge, add)
            elif not context.trick:
                self.score_lead(context, action.cards, knowledge, add)
            else:
                self.score_follow(context, action.cards, knowledge, add)
            rankings.append(RankedAction(action, tuple(reasons)))
        return ordered(rankings)

    @staticmethod
    def trump_quality(ctx, trump):
        rules = Rules(ctx.level, trump)
        trumps = [c for c in ctx.hand if rules.suit(c) == "T"]
        return len(trumps), len(rules.pair_groups(trumps)), sum(c.rank == ctx.level or c.suit == "J" for c in trumps)

    def score_bid(self, ctx, action, add):
        if action.kind == "pass":
            return
        value, trump = bid_value(action.cards, ctx.level)
        length, pairs, controls = self.trump_quality(ctx, trump)
        add("bid_length", length)
        add("bid_pairs", pairs)
        add("bid_controls", controls)
        # Assess only cards already drawn; scale the entry threshold with hand
        # size so the AI can declare early instead of waiting for all 25 cards.
        add("bid_threshold", min(1.0, len(ctx.hand) / 25))
        if ctx.bid:
            old = self.trump_quality(ctx, ctx.trump)
            improvement = length + 1.5 * pairs - old[0] - 1.5 * old[1]
            add("bid_improvement", improvement)
            if ctx.bid.seat == ctx.partner:
                add("respect_partner_bid")
            elif ctx.bid.seat == ctx.seat:
                add("reinforce")

    def score_bury(self, ctx, cards, knowledge, add):
        r = ctx.rules
        remaining = [c for c in ctx.hand if c not in cards]
        counts = Counter(r.suit(c) for c in ctx.hand)
        kept = Counter(r.suit(c) for c in remaining)
        add("bury_trump", sum(r.suit(c) == "T" for c in cards))
        add("bury_master", sum(r.suit(c) != "T" and knowledge.master((c,)) for c in cards))
        trumps = [c for c in remaining if r.suit(c) == "T"]
        strong = len(trumps) >= 10 and sum(r.strength(c) >= 12 for c in trumps) >= 3
        add("bury_points", sum(c.points for c in cards) * (0.5 if strong else 1.0))
        add("bury_pair", len(r.pair_groups(cards)))
        split, links = knowledge.damage(cards)
        add("break_pair", split)
        add("break_tractor", links)
        add("make_void", sum(counts[s] > 0 and not kept[s] for s in "SHCD"))
        add("concentrate", sum(kept[s] ** 2 for s in "SHCD"))
        add("spend_strength", sum(r.strength(c) for c in cards) * 0.4)

    def score_lead(self, ctx, cards, knowledge, add):
        r = ctx.rules
        suit = r.suit(cards[0])
        master = knowledge.master(cards)
        risk = knowledge.ruff_risk(cards, ctx.enemies)
        strength = sum(r.strength(c) for c in cards) / len(cards)
        points = sum(c.points for c in cards)
        split, links = knowledge.damage(cards)
        add("break_pair", split)
        add("break_tractor", links)
        add("lead_combo", len(cards) - 1)
        add("lead_strength", strength)
        add("ruff_risk", risk)
        if master:
            add("cash_master", 1.0 if suit != "T" else 0.45)
            add("safe_lead_points", points * (1 - risk))
            if suit != "T":
                add("cash_ace", any(c.rank == 14 for c in cards))
                length = sum(r.suit(c) == suit for c in ctx.hand)
                add("short_master", 1 / (1 + length - len(cards)))
        else:
            add("unsafe_lead_points", points)
        if suit == "T":
            add("lead_trump")
            trumps = [c for c in ctx.hand if r.suit(c) == "T"]
            outside = sum(r.suit(c) == "T" for c in knowledge.unseen)
            strong = len(trumps) >= max(7, outside / 2) and sum(r.strength(c) >= 12 for c in trumps) >= 3
            if strong and outside and (master or strength >= 11):
                add("draw_trump")
        elif (len(cards) == 1 and suit in knowledge.voids[ctx.partner]
              and knowledge.suit_pool(ctx.partner, "T")):
            add("partner_ruff", 1 - risk)
        self.retain_bottom_control(ctx, cards, knowledge, add)

    def score_follow(self, ctx, cards, knowledge, add):
        r = ctx.rules
        lead = ctx.trick[0].cards
        table = ctx.table
        before = r.winner(table)
        winner = r.winner(table + [(ctx.seat, list(cards))])
        winner_cards = cards if winner == ctx.seat else next(p.cards for p in ctx.trick if p.seat == winner)
        future_enemies = tuple(s for s in ctx.remaining_seats if s % 2 != ctx.seat % 2)
        team_wins = winner % 2 == ctx.seat % 2
        confidence = knowledge.security(winner_cards, lead, future_enemies) if team_wins else 0.0
        own_points = sum(c.points for c in cards)
        table_points = sum(c.points for p in ctx.trick for c in p.cards)
        points = own_points + table_points
        split, links = knowledge.damage(cards)
        add("break_pair", split)
        add("break_tractor", links)
        add("spend_strength", sum(r.strength(c) for c in cards))
        add("spend_trump", sum(r.suit(c) == "T" for c in cards))
        add("spend_joker", sum(c.suit == "J" for c in cards))
        if team_wins:
            add("win_control", confidence)
            add("capture_points", points * confidence)
            add("lose_points", own_points * (1 - confidence))
            if winner == ctx.partner and confidence >= 0.85:
                add("feed_partner", own_points)
            if before == ctx.partner and winner == ctx.seat:
                previous_cards = next(p.cards for p in ctx.trick if p.seat == before)
                previous_safety = knowledge.security(previous_cards, lead, future_enemies)
                if confidence - previous_safety < 0.15:
                    add("overtake_partner")
        else:
            add("lose_points", own_points)
        ruffing = r.suit(lead[0]) != "T" and all(r.suit(c) == "T" for c in cards)
        last = len(ctx.hand) == len(cards)
        if ruffing and not points and not last:
            add("empty_ruff")
        # Thresholds reflect this table's two-deck scoring (not six-player guides).
        threshold = next(t for t in (5, 40, 80, *range(120, 2000, 40)) if t > ctx.score)
        if ctx.score + points >= threshold:
            add("point_threshold", confidence if team_wins else -1.0)
        if last:
            multiplier = r.bottom_multiplier(list(lead))
            bottom = sum(c.points for c in ctx.known_bottom) if ctx.known_bottom else 15
            outcome = (2 * confidence - 1) if team_wins else -1.0
            add("last_trick", outcome)
            add("bottom_value", bottom * multiplier * outcome)
        else:
            self.retain_bottom_control(ctx, cards, knowledge, add)
            suits = Counter(r.suit(c) for c in ctx.hand)
            used = Counter(r.suit(c) for c in cards)
            add("shed_suit", sum(suits[s] and used[s] == suits[s] for s in "SHCD"))

    def retain_bottom_control(self, ctx, cards, knowledge, add):
        if not len(cards) < len(ctx.hand) <= 6:
            return
        r = ctx.rules
        trumps = [c for c in ctx.hand if r.suit(c) == "T"]
        if trumps and all(c in cards for c in trumps):
            add("keep_last_trump", 1.0 if ctx.defending else 0.8)
