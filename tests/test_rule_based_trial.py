"""Strategic regressions and information boundaries for the isolated challenger."""
from dataclasses import replace
from itertools import combinations

import pytest

from levelup.ai import STRATEGIES, observe
from levelup.ai.candidates import generate_candidates
from levelup.ai.interface import Bid, Play
from levelup.ai.knowledge import Knowledge
from levelup.ai.rule_based import RuleBasedStrategy
from levelup.ai.rule_based_trial import CountingKnowledge, TrialRuleBasedStrategy, avoiding
from levelup.ai.simulation import finish_ai_deal
from levelup.game import Game
from tests.test_ai import cards, context


def test_trial_is_not_exposed_to_live_games():
    assert "rule_based_trial" not in STRATEGIES
    assert type(STRATEGIES["rule_based"]) is RuleBasedStrategy


@pytest.mark.parametrize("total,targets,draws", [(6, 2, 3), (6, 0, 2), (6, 5, 3), (0, 0, 0)])
def test_counting_probability_matches_exhaustive_small_decks(total, targets, draws):
    deals = list(combinations(range(total), draws))
    expected = sum(all(card >= targets for card in deal) for deal in deals) / len(deals)
    assert avoiding(total, targets, draws) == pytest.approx(expected)


def test_overruff_probability_requires_following_the_original_suit(monkeypatch):
    ctx = context("S5 C3", seat=1, counts=(2, 2, 2, 2), trick=(Play(0, cards("H4")),))
    knowledge = CountingKnowledge(ctx)
    pool = cards("S8 H3 H7 C4 C9 J15")
    monkeypatch.setattr(knowledge, "pool", lambda seat: pool)
    lead, winner = ctx.trick[0].cards, ctx.hand[:1]
    beaten = 0
    deals = list(combinations(pool, 2))
    for hand in deals:
        follows = [c for c in hand if ctx.rules.suit(c) == "H"]
        legal = follows or hand
        beaten += any(ctx.rules.winner([(0, list(lead)), (1, list(winner)), (2, [c])]) == 2
                      for c in legal)
    assert knowledge.security(winner, lead, (2,)) == pytest.approx(1 - beaten / len(deals))
    # Previously every possible higher trump counted, even when it couldn't be played.
    old = Knowledge(ctx)
    monkeypatch.setattr(old, "pool", lambda seat: pool)
    assert knowledge.security(winner, lead, (2,)) > old.security(winner, lead, (2,))


def test_pair_follow_reveals_no_remaining_pairs():
    lead = cards("H8 H8")
    ctx = context("C9 S3", seat=0, history=((Play(0, lead), Play(1, cards("H3 H5"))),))
    knowledge = CountingKnowledge(ctx)
    assert "H" in knowledge.no_pairs[1]
    assert knowledge.beating(1, cards("H6 H6"), "H", 3) == 0
    assert knowledge.beating(2, cards("H6 H6"), "H", 3) > 0
    # Ordinary single-card play says nothing about possession of pairs.
    single = replace(ctx, history=((Play(0, lead[:1]), Play(1, cards("H3"))),))
    assert not CountingKnowledge(single).no_pairs[1]


def test_exposed_non_dealer_card_is_fixed_but_dealer_could_bury_it(monkeypatch):
    pool = cards("S2 S8 H3 H7 C4 C9")
    ctx = context("D9", counts=(1, 2, 2, 2), dealer=0,
                  declarations=(Bid(2, 1, pool[:1]),))
    knowledge = CountingKnowledge(ctx)
    monkeypatch.setattr(knowledge, "pool", lambda seat: pool)
    assert knowledge.holding(2, (pool[0].id,)) == 1
    assert knowledge.holding(2, (pool[0].id,), "T") == 0
    dealer = CountingKnowledge(replace(ctx, dealer=2))
    monkeypatch.setattr(dealer, "pool", lambda seat: pool)
    assert dealer.holding(2, (pool[0].id,)) == pytest.approx(2 / 6)


@pytest.mark.parametrize("own_wins", [False, True])
def test_partner_rescue_matches_exhaustive_legal_single_responses(monkeypatch, own_wins):
    ctx = context("H6 C3", seat=1, counts=(2, 2, 2, 2), trick=(Play(0, cards("H4")),))
    knowledge = CountingKnowledge(ctx)
    pools = {2: cards("H8 H10 S3 C5"), 3: cards("H9 H11 S9 D5")}
    monkeypatch.setattr(knowledge, "pool", lambda seat: pools[seat])
    lead = ctx.trick[0].cards
    own = cards("H6" if own_wins else "H3")
    wins = total = 0
    for enemy in combinations(pools[2], 2):
        for partner in combinations(pools[3], 2):
            table = [(0, list(lead)), (1, list(own))]
            for seat, hand in ((2, enemy), (3, partner)):
                follows = [c for c in hand if ctx.rules.suit(c) == "H"]
                legal = follows or hand
                chosen = max(legal, key=lambda c: ctx.rules.strength(c) +
                             (100 if ctx.rules.suit(c) == "T" else 0))
                table.append((seat, [chosen]))
            wins += ctx.rules.winner(table) % 2 == 1
            total += 1
    winner = own if own_wins else lead
    safety = knowledge.security(winner, lead, (2,)) if own_wins else 0
    assert safety + knowledge.partner_rescue(winner, lead, own_wins) == pytest.approx(wins / total)


@pytest.mark.parametrize("variant", ["full", "counting", "tactics", "partnership", "initiative"])
def test_bidding_and_burying_identical_and_hidden_hands_irrelevant(variant):
    game = Game(92)
    game.start()
    for _ in range(40):
        game.draw_card()
    trial, baseline = TrialRuleBasedStrategy(variant=variant), RuleBasedStrategy()
    ctx = observe(game, 0)
    candidates = generate_candidates(ctx)
    assert trial.rank(ctx, candidates) == baseline.rank(ctx, candidates)
    finish_ai_deal(game)
    ctx = observe(game, game.dealer)
    candidates = generate_candidates(ctx)
    assert trial.rank(ctx, candidates) == baseline.rank(ctx, candidates)
    action = baseline.rank(ctx, candidates)[0].action
    game.act(game.dealer, action.kind, [c.id for c in action.cards])
    ctx = observe(game, game.dealer)
    candidates = generate_candidates(ctx)
    before = trial.rank(ctx, candidates)
    assert len(before) == len(candidates)
    assert {r.action.key for r in before} == {c.key for c in candidates}
    others = [i for i in range(4) if i != game.dealer]
    game.hands[others[0]], game.hands[others[1]] = game.hands[others[1]], game.hands[others[0]]
    assert ctx == observe(game, game.dealer)
    assert trial.rank(observe(game, game.dealer), candidates) == before


@pytest.mark.parametrize("seed", [5, 10, 13, 14])
def test_trial_completes_legal_rounds_with_every_ranked_candidate(seed):
    game = Game(seed)
    game.levels = [seed, seed]
    game.start()
    finish_ai_deal(game)
    trial = TrialRuleBasedStrategy(variant="initiative")
    action, ids = game.ai_action(game.turn)
    game.act(game.turn, action, ids)
    while game.phase == "playing":
        ctx = observe(game, game.turn)
        candidates = generate_candidates(ctx)
        ranked = trial.rank(ctx, candidates)
        assert len(ranked) == len(candidates)
        assert all(r.score == r.score for r in ranked)
        if ctx.trick:
            for candidate in ranked:
                ctx.rules.validate_follow(ctx.hand, candidate.action.cards, ctx.trick[0].cards)
        action = ranked[0].action
        game.act(game.turn, "play", [c.id for c in action.cards])
    assert all(not hand for hand in game.hands)


def test_getting_in_to_cash_aces_can_justify_ruffing_an_empty_trick():
    ctx = context("S3 H14 C14 D3", seat=3,
                  trick=(Play(0, cards("D4")), Play(1, cards("D6")), Play(2, cards("D8"))))
    # Remove the led suit, otherwise trumping would be illegal.
    ctx = replace(ctx, hand=cards("S3 H14 C14"))
    ranked = TrialRuleBasedStrategy(variant="initiative").rank(ctx, generate_candidates(ctx))
    assert ranked[0].action.cards == cards("S3")
    assert any(r.rule == "entry_to_winners" and r.value > 0 for r in ranked[0].reasons)


def test_paired_statistics_do_not_treat_swapped_games_as_independent():
    from levelup.bench_rules import summarize
    rows = [{"seed": seed, "trial_team": team, "trial_won": team == 0,
             "trial_defending": team == 0, "signed_points": 0}
            for seed in range(256) for team in (0, 1)]
    result = summarize(rows)
    assert result["win_rate"] == .5
    assert result["paired_bootstrap_95_ci"] == [.5, .5]
    assert not result["passes_gate"]
    with pytest.raises(ValueError, match="exactly once"):
        summarize(rows + [rows[0]])
