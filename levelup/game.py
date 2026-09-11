"""Authoritative two-deck Tractor rules. No networking or wall-clock dependencies."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from itertools import product
import random

SUITS = "SHCD"
SYMBOLS = {"S": "♠", "H": "♥", "C": "♣", "D": "♦", "J": "王"}
RANKS = {**{i: str(i) for i in range(2, 11)}, 11: "J", 12: "Q", 13: "K", 14: "A", 15: "小王", 16: "大王"}


class RuleError(ValueError):
    pass


def bid_value(cards, level):
    if len(cards) not in (1, 2) or len({c.key for c in cards}) != 1:
        raise RuleError("亮主需一张级牌、一对同花色级牌或一对同色王。")
    c = cards[0]
    if c.suit == "J" and len(cards) == 2:
        return c.rank - 12, None
    if c.rank == level:
        return len(cards), c.suit
    raise RuleError("只能用当前级牌或一对王亮主。")


@dataclass(frozen=True)
class Card:
    id: int
    suit: str
    rank: int

    @property
    def key(self):
        return self.suit, self.rank

    @property
    def points(self):
        return 5 if self.rank == 5 else 10 if self.rank in (10, 13) else 0

    def json(self):
        return {"id": self.id, "suit": self.suit, "rank": self.rank,
                "label": RANKS[self.rank], "symbol": SYMBOLS[self.suit], "points": self.points}


def deck():
    faces = [(s, r) for s in SUITS for r in range(2, 15)] + [("J", 15), ("J", 16)]
    return [Card(i, *face) for i, face in enumerate(faces * 2)]


@dataclass
class Component:
    cards: list[Card]
    pairs: int  # 0 = single, 1 = pair, >=2 = tractor
    strength: int


class Rules:
    def __init__(self, level=2, trump="S"):
        self.level, self.trump = level, trump

    def suit(self, c):
        return "T" if c.suit == "J" or c.rank == self.level or c.suit == self.trump else c.suit

    def strength(self, c):
        # Compressed ordinary ranks: the level card leaves no hole in tractors.
        if c.suit == "J":
            return (14 if self.trump else 13) + c.rank - 15
        if c.rank == self.level:
            return 13 if c.suit == self.trump else 12
        return [r for r in range(2, 15) if r != self.level].index(c.rank)

    def sort(self, cards):
        return sorted(cards, key=lambda c: ("SHCDT".index(self.suit(c)), self.strength(c), c.suit, c.id))

    def pair_groups(self, cards):
        groups = defaultdict(list)
        for c in cards:
            groups[c.key].append(c)
        return sorted([v for v in groups.values() if len(v) == 2],
                      key=lambda v: (self.strength(v[0]), v[0].suit))

    def runs(self, pairs, length):
        """All exact-length runs; equally ranked off-suit level pairs are alternatives."""
        by_rank = defaultdict(list)
        for i, p in enumerate(pairs):
            by_rank[self.strength(p[0])].append(i)
        for start in sorted(by_rank):
            if all(start + j in by_rank for j in range(length)):
                yield from product(*(by_rank[start + j] for j in range(length)))

    def components(self, cards):
        if not cards or len({self.suit(c) for c in cards}) != 1:
            raise RuleError("领出的牌必须属于同一门花色（主牌算一门）。")
        remaining = list(cards)
        result = []
        while pairs := self.pair_groups(remaining):
            run = None
            for length in range(len(pairs), 0, -1):
                run = next(self.runs(pairs, length), None)
                if run is not None:
                    break
            chosen = [c for i in run for c in pairs[i]]
            result.append(Component(chosen, len(run), max(self.strength(c) for c in chosen)))
            removed = {c.id for c in chosen}
            remaining = [c for c in remaining if c.id not in removed]
        result.extend(Component([c], 0, self.strength(c)) for c in self.sort(remaining))
        return result

    def follow_plan(self, cards, demands):
        """Optimal mandatory pair structure, plus a witness for AI/hints.

        For each led tractor (longest first), preserve the longest possible run,
        then its remainder; finally preserve remaining led pairs. Search all tied
        allocations so a greedy choice cannot consume a later required tractor.
        A signature reserves one position per demanded pair, making comparisons
        deterministic even when a tractor must be split.
        """
        pairs = self.pair_groups(cards)
        runs = {n: list(self.runs(pairs, n)) for n in range(1, max(demands, default=0) + 1)}

        @lru_cache(None)
        def solve(mask, todo):
            if not todo:
                return (), ()
            k, *tail = todo
            for n in range(k, 0, -1):
                choices = []
                for run in runs[n]:
                    bits = sum(1 << i for i in run)
                    if mask & bits != bits:
                        continue
                    rest = ((k - n,) if k > n else ()) + tuple(tail)
                    score, indices = solve(mask ^ bits, rest)
                    choices.append(((n,) + (0,) * (n - 1) + score, tuple(run) + indices))
                if choices:
                    return max(choices, key=lambda x: x[0])
            return (0,) * sum(todo), ()

        score, indices = solve((1 << len(pairs)) - 1, tuple(demands))
        return score, [c for i in indices for c in pairs[i]]

    def validate_follow(self, hand, played, lead):
        n = len(lead)
        if len(played) != n:
            raise RuleError(f"本墩必须出 {n} 张牌。")
        suit = self.suit(lead[0])
        available = [c for c in hand if self.suit(c) == suit]
        matching = [c for c in played if self.suit(c) == suit]
        if len(matching) != min(n, len(available)):
            raise RuleError("必须先跟足领出花色；主牌算独立的一门。")
        if len(available) >= n:
            demands = [p.pairs for p in self.components(lead) if p.pairs]
            if self.follow_plan(matching, demands)[0] != self.follow_plan(available, demands)[0]:
                raise RuleError("有拖拉机须跟拖拉机；不足时先跟最长连对，再尽量跟足对子。")

    def shape_strength(self, cards, lead):
        """Return a matching shape's decisive strength, or None for a discard."""
        if len(cards) != len(lead) or len({self.suit(c) for c in cards}) != 1:
            return None
        parts = self.components(lead)
        demands = tuple(p.pairs for p in parts if p.pairs)
        pairs = self.pair_groups(cards)
        runs = {n: list(self.runs(pairs, n)) for n in set(demands)}

        @lru_cache(None)
        def match(mask, pos):
            if pos == len(demands):
                return ()
            choices = []
            for run in runs[demands[pos]]:
                bits = sum(1 << i for i in run)
                if mask & bits == bits:
                    rest = match(mask ^ bits, pos + 1)
                    if rest is not None:
                        choices.append((max(self.strength(pairs[i][0]) for i in run),) + rest)
            return max(choices) if choices else None

        result = match((1 << len(pairs)) - 1, 0)
        if result is None:
            return None
        # Throw overruffs compare the largest combination, not its loose singles.
        return result[0] if result else max(self.strength(c) for c in cards)

    def winner(self, trick):
        leader, lead = trick[0]
        suit = self.suit(lead[0])
        best_seat, best_suit = leader, suit
        best = self.shape_strength(lead, lead)
        thrown = len(self.components(lead)) > 1
        for seat, cards in trick[1:]:
            strength = self.shape_strength(cards, lead)
            category = self.suit(cards[0])
            if strength is None or category not in (suit, "T"):
                continue
            # A successful same-suit throw cannot be beaten by that same suit.
            if thrown and category == suit:
                continue
            if ((category == "T" and best_suit != "T") or
                    (category == best_suit and strength > best)):
                best_seat, best_suit, best = seat, category, strength
        return best_seat

    def failed_throw(self, cards, other_hands):
        parts = self.components(cards)
        if len(parts) == 1:
            return None
        suit = self.suit(cards[0])
        vulnerable = []
        for part in parts:
            for hand in other_hands:
                suited = [c for c in hand if self.suit(c) == suit]
                if part.pairs == 0:
                    beaten = any(self.strength(c) > part.strength for c in suited)
                else:
                    pairs = self.pair_groups(suited)
                    beaten = any(max(self.strength(pairs[i][0]) for i in run) > part.strength
                                 for run in self.runs(pairs, part.pairs))
                if beaten:
                    vulnerable.append(part)
                    break
        return min(vulnerable, key=lambda p: (len(p.cards), p.strength)).cards if vulnerable else None

    def bottom_multiplier(self, lead):
        parts = self.components(lead)
        largest = max(p.pairs for p in parts)
        if largest:
            return 2 * (largest + 1)  # pair 4, two-pair tractor 6, three-pair 8
        return 3 if len(parts) > 1 else 2


class Game:
    def __init__(self, seed=None):
        self.rng = random.Random(seed)
        self.phase = "lobby"
        self.levels = [2, 2]
        self.dealer = 0
        self.round = 0
        self.version = 0
        self.hands = [[] for _ in range(4)]
        self.bottom = []
        self.draw_pile = []
        self.dealt = 0
        self.deal_id = 0
        self.last_draw_seat = None
        self.trump = None
        self.level = 2
        self.turn = 0
        self.bid = None
        self.declarations = []
        self.bid_passed = set()
        self.trick = []
        self.last_trick = None
        self.score = 0
        self.history = []
        self.result = None
        self.events = []

    @property
    def rules(self):
        return Rules(self.level, self.trump)

    def log(self, text):
        self.events.append(text)
        self.events = self.events[-40:]

    def start(self):
        if self.phase not in ("lobby", "round_end"):
            raise RuleError("当前牌局尚未结束。")
        self.round += 1
        self.level = self.levels[self.dealer % 2]
        self.score = 0
        self.bid, self.result, self.last_trick = None, None, None
        self.trump = None
        self.trick = []
        self.history = []
        self.turn = self.dealer
        self.deal()
        self.version += 1
        self.log(f"第 {self.round} 局开始，打 {RANKS[self.level]}。摸牌期间可随时亮主或反主。")

    def deal(self):
        self.phase = "dealing"
        self.deal_id = self.version + 1
        self.dealt = 0
        self.last_draw_seat = None
        self.bid, self.trump = None, None
        self.declarations = []
        self.bid_passed = set()
        cards = deck()
        for _ in range(3):
            self.rng.shuffle(cards)
        self.hands = [[] for _ in range(4)]
        self.draw_pile = cards[:100]
        self.bottom = cards[100:]
        self.turn = self.dealer

    def draw_card(self):
        """One authoritative draw; the room clock decides when to call this."""
        if self.phase != "dealing" or not self.draw_pile:
            raise RuleError("当前不能摸牌。")
        seat = self.turn
        self.hands[seat].append(self.draw_pile.pop(0))
        self.dealt += 1
        self.last_draw_seat = seat
        self.turn = (seat + 1) % 4
        self.version += 1
        return seat

    def get_cards(self, seat, ids):
        if not isinstance(ids, list) or not ids or any(type(i) is not int for i in ids):
            raise RuleError("请选择要出的牌。")
        if len(ids) != len(set(ids)):
            raise RuleError("不能重复选择同一张牌。")
        by_id = {c.id: c for c in self.hands[seat]}
        if any(i not in by_id for i in ids):
            raise RuleError("所选牌不在你的手牌中，请重新选择。")
        return [by_id[i] for i in ids]

    def bid_value(self, cards):
        return bid_value(cards, self.level)

    def declare(self, seat, ids):
        cards = self.get_cards(seat, ids)
        value, trump = self.bid_value(cards)
        if self.bid:
            if value <= self.bid["value"]:
                raise RuleError("反主必须更强：单级牌 < 对级牌 < 对小王 < 对大王。")
            if self.bid["seat"] == seat and not (trump == self.trump and value == 2):
                raise RuleError("不能反自己，只能用同花色的一对级牌加固。")
        self.bid = {"seat": seat, "value": value, "cards": [c.json() for c in cards]}
        self.trump = trump
        self.declarations.append(self.bid)
        self.bid_passed.clear()
        self.log(f"{seat + 1} 号位亮主：{SYMBOLS.get(trump, '无主')}。")

    def finish_dealing(self):
        """Close simultaneous bidding after the last-card reaction window."""
        if self.phase != "dealing" or self.draw_pile:
            raise RuleError("尚未摸完，不能结束亮主。")
        if not self.bid and self.round == 1:
            self.deal()
            self.version += 1
            self.log("首局无人亮主，重新摸牌。")
            return
        if self.bid and self.round == 1:
            self.dealer = self.bid["seat"]
        if not self.bid:
            self.trump = next((c.suit for c in self.bottom if c.suit != "J"), "S")
            self.log(f"无人亮主，翻底定主：{SYMBOLS[self.trump]}。")
        self.hands[self.dealer].extend(self.bottom)
        self.bottom = []
        self.phase, self.turn = "burying", self.dealer
        self.version += 1
        self.log(f"{self.dealer + 1} 号位坐庄，请扣下 8 张底牌。")

    def bury(self, seat, ids):
        cards = self.get_cards(seat, ids)
        if len(cards) != 8:
            raise RuleError("必须扣下恰好 8 张底牌。")
        self.bottom = cards
        self.remove(seat, cards)
        self.phase = "playing"
        self.log("扣底完成，庄家领出。")

    def remove(self, seat, cards):
        ids = {c.id for c in cards}
        self.hands[seat] = [c for c in self.hands[seat] if c.id not in ids]

    def play(self, seat, ids):
        cards = self.get_cards(seat, ids)
        rules = self.rules
        if not self.trick:
            rules.components(cards)
            forced = rules.failed_throw(cards, [h for i, h in enumerate(self.hands) if i != seat])
            if forced:
                cards = forced
                self.log(f"{seat + 1} 号位甩牌失败，强制出最小失败组合（不罚分）。")
        else:
            rules.validate_follow(self.hands[seat], cards, self.trick[0][1])
        self.remove(seat, cards)
        self.trick.append((seat, rules.sort(cards)))
        self.turn = (seat + 1) % 4
        if len(self.trick) == 4:
            winner = rules.winner(self.trick)
            points = sum(c.points for _, cs in self.trick for c in cs)
            if winner % 2 != self.dealer % 2:
                self.score += points
            self.last_trick = {"plays": self.serialize_trick(self.trick), "winner": winner,
                               "points": points, "number": len(self.history) + 1}
            self.history.append(self.last_trick)
            self.log(f"第 {len(self.history)} 墩：{winner + 1} 号位获胜，{points} 分。")
            if not self.hands[0]:
                self.finish(winner, self.trick[0][1])
            self.trick = []
            self.turn = winner

    def finish(self, winner, lead):
        multiplier = self.rules.bottom_multiplier(lead) if winner % 2 != self.dealer % 2 else 0
        bottom_points = sum(c.points for c in self.bottom)
        self.score += bottom_points * multiplier
        defending = self.dealer % 2
        if self.score < 80:
            team = defending
            gain = 3 if self.score == 0 else 2 if self.score < 40 else 1
            next_dealer = (self.dealer + 2) % 4
        else:
            team = 1 - defending
            gain = (self.score - 80) // 40
            next_dealer = (self.dealer + 1) % 4
        old_level = self.levels[team]
        # A must be played through while defending; reaching A does not end a match.
        champion = team if old_level == 14 and team == defending and gain > 0 else None
        self.levels[team] = min(14, old_level + gain)
        self.result = {"score": self.score, "bottom_points": bottom_points, "multiplier": multiplier,
                       "team": team, "gain": self.levels[team] - old_level,
                       "next_dealer": next_dealer, "champion": champion,
                       "dealer": self.dealer, "levels": list(self.levels)}
        self.phase = "match_end" if champion is not None else "round_end"
        self.log(f"本局闲家 {self.score} 分，{'南北' if team == 0 else '东西'}队升 {self.result['gain']} 级。")
        # Keep this round's dealer visible until starting the next round.

    @staticmethod
    def serialize_trick(trick):
        return [{"seat": seat, "cards": [c.json() for c in cards]} for seat, cards in trick]

    def act(self, seat, action, ids=None):
        if type(seat) is not int or seat not in range(4):
            raise RuleError("座位须为 0–3。")
        if self.phase != "dealing" and seat != self.turn:
            raise RuleError("还没有轮到你。")
        expected = {"dealing": ("bid", "pass"), "burying": ("bury",), "playing": ("play",)}
        if action not in expected.get(self.phase, ()):
            raise RuleError("当前阶段不能执行这个操作。")
        if action == "pass":
            if self.draw_pile:
                raise RuleError("摸牌完成后才能选择不亮。")
            if ids not in (None, []):
                raise RuleError("不亮无需选择牌。")
            self.bid_passed.add(seat)
        elif action == "bid":
            self.declare(seat, ids)
        elif action == "bury":
            self.bury(seat, ids)
        elif action == "play":
            self.play(seat, ids)
        self.version += 1

    def next_round(self):
        if self.phase != "round_end":
            raise RuleError("现在不能开始下一局。")
        self.dealer = self.result["next_dealer"]
        self.start()

    def ai_rankings(self, seat, strategy="rule_based"):
        from .ai import observe, rank_actions
        if self.phase not in ("dealing", "burying", "playing"):
            return []
        return rank_actions(observe(self, seat), strategy)

    def ai_action(self, seat, strategy="rule_based"):
        ranked = self.ai_rankings(seat, strategy)
        if not ranked:
            return None, []
        best = ranked[0].action
        return best.kind, [c.id for c in best.cards]

    def view(self, seat):
        hand = []
        for card in self.rules.sort(self.hands[seat]):
            hand.append({**card.json(), "group": self.rules.suit(card)})
        return {"phase": self.phase, "version": self.version, "round": self.round,
                "level": RANKS[self.level], "levels": [RANKS[l] for l in self.levels],
                "trump": self.trump, "dealer": self.dealer, "turn": self.turn,
                "bid": self.bid, "hand": hand,
                "bid_revision": len(self.declarations), "bid_passed": sorted(self.bid_passed),
                "deal_id": self.deal_id, "dealt": self.dealt, "deal_total": 100,
                "last_draw_seat": self.last_draw_seat,
                "counts": [len(h) for h in self.hands], "score": self.score,
                "trick": self.serialize_trick(self.trick), "last_trick": self.last_trick,
                "trick_number": len(self.history) + 1, "result": self.result,
                "bottom": [c.json() for c in self.bottom] if self.phase in ("round_end", "match_end") else [],
                "events": self.events[-16:]}
