"""Strategy registry and safe observation adapter for the game engine."""
from ..game import Card, RuleError
from .interface import AIContext, AIStrategy, Action, Bid, Play, RankedAction
from .basic import BasicStrategy
from .rule_based import RuleBasedStrategy

DEFAULT_STRATEGY = "rule_based"
STRATEGIES: dict[str, AIStrategy] = {}


def register(strategy: AIStrategy):
    if not isinstance(strategy, AIStrategy):
        raise TypeError("Strategy must implement AIStrategy")
    if strategy.id in STRATEGIES:
        raise ValueError(f"Strategy already registered: {strategy.id}")
    STRATEGIES[strategy.id] = strategy


register(BasicStrategy())
register(RuleBasedStrategy())


def get_strategy(strategy_id):
    if not isinstance(strategy_id, str) or strategy_id not in STRATEGIES:
        raise RuleError("未知 AI 策略，请从列表中选择。")
    return STRATEGIES[strategy_id]


def strategy_catalog():
    return [{"id": s.id, "name": s.name, "description": s.description} for s in STRATEGIES.values()]


def observe(game, seat):
    """The only adapter allowed to read Game; it copies a player's information set."""
    def card(data):
        return Card(data["id"], data["suit"], data["rank"])

    def bid(data):
        return Bid(data["seat"], data["value"], tuple(card(c) for c in data["cards"]))

    return AIContext(
        seat=seat, phase=game.phase, hand=tuple(game.hands[seat]), level=game.level,
        trump=game.trump, dealer=game.dealer, score=game.score,
        counts=tuple(len(h) for h in game.hands),
        trick=tuple(Play(i, tuple(cs)) for i, cs in game.trick),
        history=tuple(tuple(Play(p["seat"], tuple(card(c) for c in p["cards"])) for p in t["plays"])
                      for t in game.history),
        bid=bid(game.bid) if game.bid else None,
        declarations=tuple(bid(b) for b in game.declarations),
        known_bottom=tuple(game.bottom) if seat == game.dealer and game.phase == "playing" else (),
    )


def rank_actions(context, strategy_id=DEFAULT_STRATEGY):
    from .candidates import generate_candidates
    strategy = get_strategy(strategy_id)
    candidates = generate_candidates(context)
    ranked = strategy.rank(context, candidates)
    if (len(ranked) != len(candidates) or
            {r.action.key for r in ranked} != {a.key for a in candidates}):
        raise ValueError("Strategy must rank each supplied candidate exactly once")
    return ranked
