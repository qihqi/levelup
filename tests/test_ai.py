from dataclasses import FrozenInstanceError, replace
import gc
import math
import weakref

import pytest

from levelup.ai import observe, rank_actions
from levelup.ai.candidates import generate_candidates
from levelup.ai.interface import AIContext, AIStrategy, Action, Bid, Play
from levelup.ai.knowledge import Knowledge
from levelup.ai.rule_based import RULES, RuleBasedStrategy
from levelup.game import Game, Rules, deck
from levelup.ai.simulation import finish_ai_deal


def cards(spec):
    available = deck()
    result = []
    for face in spec.split():
        c = next(c for c in available if c.suit == face[0] and c.rank == int(face[1:]))
        result.append(c)
        available.remove(c)
    return tuple(result)


def context(spec, **kwargs):
    defaults = dict(seat=0, phase="playing", hand=cards(spec), level=2, trump="S",
                    dealer=0, score=0, counts=(25, 25, 25, 25))
    defaults.update(kwargs)
    return AIContext(**defaults)


def best(ctx, strategy="rule_based"):
    return rank_actions(ctx, strategy)[0]


def faces(action):
    return sorted(c.key for c in action.cards)


def test_lead_ace_before_low_pair():
    ctx = context("H14 C4 C4 D7 S3")
    assert faces(best(ctx).action) == [("H", 14)]
    assert faces(best(ctx, "basic").action) == [("C", 4), ("C", 4)]


def test_king_promoted_only_when_both_aces_accounted_for():
    ctx = context("H13 C6")
    aces = cards("H14 H14")
    one_seen = replace(ctx, history=((Play(1, aces[:1]),),))
    both_seen = replace(ctx, history=((Play(1, aces),),))
    assert not Knowledge(ctx).master(cards("H13"))
    assert not Knowledge(one_seen).master(cards("H13"))
    assert Knowledge(both_seen).master(cards("H13"))
    assert faces(best(both_seen).action) == [("H", 13)]
    assert "cash_master" in {r.rule for r in best(both_seen).reasons}


def test_trump_level_ace_does_not_get_side_ace_bonus():
    ctx = context("H14 C13 D3", level=14)
    ranked = rank_actions(ctx)
    ace = next(r for r in ranked if faces(r.action) == [("H", 14)])
    assert "cash_ace" not in {r.rule for r in ace.reasons}
    assert faces(ranked[0].action) == [("C", 13)]


def test_preserve_ace_pair_instead_of_splitting_it():
    ranked = best(context("H14 H14 C3 S4"))
    assert faces(ranked.action) == [("H", 14), ("H", 14)]


def test_avoid_ace_when_enemy_known_void_and_can_ruff():
    history = ((Play(0, cards("H4")), Play(1, cards("S5"))),)
    ctx = context("H14 C14 D3", history=history)
    knowledge = Knowledge(ctx)
    assert "H" in knowledge.voids[1]
    assert faces(best(ctx).action) == [("C", 14)]
    unsafe = next(r for r in rank_actions(ctx) if faces(r.action) == [("H", 14)])
    assert any(r.rule == "ruff_risk" and r.value < -100 for r in unsafe.reasons)


def test_same_suit_high_discard_is_not_proof_of_void():
    ctx = context("H13 C3", history=((Play(0, cards("H14")), Play(1, cards("H12"))),))
    assert "H" not in Knowledge(ctx).voids[1]


def test_known_void_enemy_without_trumps_cannot_ruff():
    history = ((Play(0, cards("H4")), Play(1, cards("C5"))),
               (Play(0, cards("S4")), Play(1, cards("D5"))),
               (Play(0, cards("S6")), Play(3, cards("D6"))))
    ctx = context("H14 C3", history=history)
    assert Knowledge(ctx).ruff_risk(cards("H14"), ctx.enemies) == 0
    assert faces(best(ctx).action) == [("H", 14)]


def test_feed_points_to_safe_partner_when_last_to_act():
    ctx = context("H10 H3 H12", seat=3,
                  trick=(Play(0, cards("H4")), Play(1, cards("H14")), Play(2, cards("H8"))))
    ranked = best(ctx)
    assert faces(ranked.action) == [("H", 10)]
    assert any(r.rule == "feed_partner" and r.value > 0 for r in ranked.reasons)


def test_do_not_overtake_safe_partner_or_feed_an_unsafe_one():
    safe = context("H14 H10 H3", seat=3,
                   trick=(Play(0, cards("H4")), Play(1, cards("H13")), Play(2, cards("H8"))))
    assert faces(best(safe).action) == [("H", 10)]
    unsafe = context("C10 C3 S14", seat=2,
                     trick=(Play(0, cards("H14")), Play(1, cards("H3"))),
                     history=((Play(0, cards("H5")), Play(3, cards("S4"))),))
    assert faces(best(unsafe).action) == [("C", 3)]


def test_last_player_uses_smallest_card_that_wins_points():
    ctx = context("H11 H14 H3", seat=3,
                  trick=(Play(0, cards("H10")), Play(1, cards("H4")), Play(2, cards("H7"))))
    assert faces(best(ctx).action) == [("H", 11)]


def test_discard_instead_of_ruffing_empty_trick():
    ctx = context("C3 S14 S4 H7 D8 D9 C6", seat=3,
                  trick=(Play(0, cards("D14")), Play(1, cards("D4")), Play(2, cards("D7"))))
    # Remove the lead suit to offer a real ruff-versus-discard choice.
    ctx = replace(ctx, hand=cards("C3 S14 S4 H7 H8 H9 C6"))
    assert ctx.rules.suit(best(ctx).action.cards[0]) != "T"


def test_last_trick_uses_trump_even_without_visible_points():
    ctx = context("S14", seat=3, counts=(0, 0, 0, 1),
                  trick=(Play(0, cards("D14")), Play(1, cards("D4")), Play(2, cards("D7"))))
    ranked = best(ctx)
    assert faces(ranked.action) == [("S", 14)]
    assert any(r.rule == "last_trick" and r.value > 0 for r in ranked.reasons)


def test_bury_short_suit_but_keep_ace_and_pairs():
    ctx = context("H3 H4 C3 C4 C6 C8 C9 C11 D3 D4 D6 D8 D9 D14 S3 S4 S5 S6 S8 S9 S11 S12 S13 S14 H2 H2 C2 D2 J15 J15 J16 J16 D12",
                  phase="burying")
    action = best(ctx).action
    assert len(action.cards) == 8
    assert set(cards("H3 H4")) <= set(action.cards)
    assert not any(c.rank == 14 or ctx.rules.suit(c) == "T" for c in action.cards)
    assert Knowledge(ctx).damage(action.cards) == (0, 0)


def test_do_not_counterbid_partner_without_substantial_improvement():
    bid = Bid(2, 1, cards("H2"))
    ctx = context("S2 S2 S3 H3 H4 H5 H6 C3 D3 J15", phase="dealing", trump="H", bid=bid,
                  declarations=(bid,))
    assert best(ctx).action.kind == "pass"
    assert best(ctx, "basic").action.kind == "bid"


def test_points_threshold_has_explicit_scoring_term():
    ctx = context("H10 H3 H12", seat=3, dealer=0, score=70,
                  trick=(Play(0, cards("H4")), Play(1, cards("H14")), Play(2, cards("H8"))))
    ranked = best(ctx)
    assert any(r.rule == "point_threshold" and r.value > 0 for r in ranked.reasons)


def test_contract_is_immutable_and_scores_are_explainable_and_deterministic():
    ctx = context("H14 C3 C3 D5")
    with pytest.raises(FrozenInstanceError):
        ctx.score = 100
    with pytest.raises(TypeError):
        AIStrategy()
    candidates = generate_candidates(ctx)
    ranked = RuleBasedStrategy().rank(ctx, candidates)
    assert ranked == RuleBasedStrategy().rank(ctx, candidates)
    assert {r.action.key for r in ranked} == {a.key for a in candidates}
    assert [r.score for r in ranked] == sorted((r.score for r in ranked), reverse=True)
    assert all(math.isfinite(r.score) and r.score == sum(x.value for x in r.reasons) for r in ranked)
    assert all(x.rule in RULES for r in ranked for x in r.reasons)


def test_custom_weights_can_change_ranking_without_engine_changes():
    ctx = context("H14 C3 C3 D5")
    candidates = generate_candidates(ctx)
    weights = {key: 0 for key in RULES}
    weights["lead_combo"] = 100
    strategy = RuleBasedStrategy(weights)
    assert len(strategy.rank(ctx, candidates)[0].action.cards) > 1
    with pytest.raises(ValueError):
        RuleBasedStrategy({"typo": 1})


def test_knowledge_caches_do_not_retain_completed_decisions():
    knowledge = Knowledge(context("H14 C3"))
    knowledge.master(cards("H14"))
    knowledge.ruff_risk(cards("H14"), (1, 3))
    reference = weakref.ref(knowledge)
    del knowledge
    gc.collect()
    assert reference() is None


def test_strategy_cannot_see_opponent_hands_or_non_dealer_bottom():
    g = Game(7)
    g.start()
    finish_ai_deal(g)
    while g.phase != "playing":
        a, ids = g.ai_action(g.turn)
        g.act(g.turn, a, ids)
    seat = (g.dealer + 1) % 4
    snapshot = observe(g, seat)
    assert snapshot.known_bottom == ()
    assert not hasattr(snapshot, "hands") and not hasattr(snapshot, "game")
    assert observe(g, g.dealer).known_bottom == tuple(g.bottom)
    before = best(snapshot)
    # Rearrange all concealed cards, preserving public counts and the observer's hand.
    opponents = [i for i in range(4) if i != seat]
    concealed = [c for i in opponents for c in g.hands[i]] + g.bottom
    concealed.reverse()
    for i in opponents:
        n = len(g.hands[i]); g.hands[i] = concealed[:n]; del concealed[:n]
    g.bottom = concealed
    assert observe(g, seat) == snapshot
    assert best(observe(g, seat)) == before


def test_new_deal_resets_counting_and_declarations():
    g = Game(11); g.start()
    finish_ai_deal(g)
    assert g.declarations
    g.deal()
    assert not g.declarations
    assert not Knowledge(observe(g, 0)).played


def test_candidate_generation_preserves_mandatory_partial_tractors():
    ctx = context("H3 H3 H4 H4 H7 H7 H10 H10 H11 H12 H13", seat=1,
                  trick=(Play(0, cards("H8 H8 H9 H9 H11 H11")),))
    actions = generate_candidates(ctx)
    assert len(actions) > 1
    for a in actions:
        ctx.rules.validate_follow(ctx.hand, a.cards, ctx.trick[0].cards)


@pytest.mark.parametrize("strategy", ["basic", "rule_based"])
@pytest.mark.parametrize("seed", range(8))
def test_every_candidate_is_legal_through_complete_rounds(strategy, seed):
    g = Game(seed); g.start()
    actions = 0
    while g.phase not in ("round_end", "match_end"):
        if g.phase == "dealing":
            finish_ai_deal(g, (strategy,) * 4)
            continue
        ctx = observe(g, g.turn)
        ranked = rank_actions(ctx, strategy)
        for item in ranked:
            cs = item.action.cards
            assert {c.id for c in cs} <= {c.id for c in ctx.hand}
            assert len({c.id for c in cs}) == len(cs)
            if ctx.phase == "playing":
                if ctx.trick:
                    ctx.rules.validate_follow(ctx.hand, cs, ctx.trick[0].cards)
                else:
                    assert g.rules.failed_throw(cs, [h for i, h in enumerate(g.hands) if i != g.turn]) is None
            elif ctx.phase == "burying":
                assert len(cs) == 8
        a = ranked[0].action
        g.act(g.turn, a.kind, [c.id for c in a.cards])
        actions += 1
        assert actions < 200
    assert sum(map(len, g.hands)) == 0
