from collections import Counter
import itertools

import pytest

from levelup.game import Card, Game, RuleError, Rules, deck
from levelup.ai.simulation import finish_ai_deal


def cards(spec):
    """Examples: H9 H9 H11 H11 J15 J16; IDs only identify physical cards."""
    return [Card(i, face[0], int(face[1:])) for i, face in enumerate(spec.split())]


def test_deck_and_deal():
    all_cards = deck()
    assert len(all_cards) == len({c.id for c in all_cards}) == 108
    assert set(Counter(c.key for c in all_cards).values()) == {2}
    assert sum(c.points for c in all_cards) == 200
    game = Game(1)
    game.start()
    assert game.phase == "dealing" and not any(game.hands)
    assert len(game.draw_pile) == 100
    for _ in range(100):
        game.draw_card()
    assert [len(h) for h in game.hands] == [25] * 4
    assert len(game.bottom) == 8
    assert len({c.id for c in sum(game.hands, []) + game.bottom}) == 108


def test_level_trump_strength_and_equal_side_levels():
    r = Rules(10, "S")
    ordered = cards("S2 S9 S11 S14 H10 S10 J15 J16")
    assert [r.strength(c) for c in ordered] == sorted(r.strength(c) for c in ordered)
    assert r.strength(cards("H10")[0]) == r.strength(cards("D10")[0])
    assert r.suit(cards("H10")[0]) == "T"
    assert r.suit(cards("H11")[0]) == "H"
    assert Rules(10, None).suit(cards("S11")[0]) == "S"


@pytest.mark.parametrize("spec,pairs", [
    ("H9 H9 H11 H11", [2]),
    ("S14 S14 D10 D10", [2]),
    ("C10 C10 S10 S10", [2]),
    ("S10 S10 J15 J15 J16 J16", [3]),
    ("D10 D10 C10 C10", [1, 1]),
    ("H5 H5 H8 H8 H12", [1, 1, 0]),
])
def test_tractor_components(spec, pairs):
    assert [p.pairs for p in Rules(10, "S").components(cards(spec))] == pairs


def test_no_trump_level_joker_tractor():
    assert Rules(10, None).components(cards("C10 C10 J15 J15"))[0].pairs == 2
    assert len(Rules(10, None).components(cards("C10 C10 H10 H10"))) == 2


def test_follow_suit_before_discarding():
    r = Rules()
    hand = cards("H4 H8 C3 C3")
    lead = cards("H6 H6")
    with pytest.raises(RuleError, match="跟足"):
        r.validate_follow(hand, [hand[0], hand[2]], lead)
    r.validate_follow(hand, hand[:2], lead)
    r.validate_follow(hand[1:], hand[1:3], lead)


def test_must_follow_pair():
    r = Rules()
    hand = cards("H4 H4 H8 H9")
    with pytest.raises(RuleError, match="对子"):
        r.validate_follow(hand, hand[2:], cards("H6 H6"))
    r.validate_follow(hand, hand[:2], cards("H6 H6"))


def test_must_follow_tractor_before_disconnected_pairs():
    r = Rules()
    hand = cards("H4 H4 H5 H5 H8 H8")
    lead = cards("H10 H10 H11 H11")
    with pytest.raises(RuleError):
        r.validate_follow(hand, hand[:2] + hand[4:], lead)
    r.validate_follow(hand, hand[:4], lead)


def test_partial_tractor_and_maximum_pair_requirement():
    r = Rules()
    hand = cards("H4 H4 H5 H5 H8 H8 H10 H10 H13 H14")
    lead = cards("H11 H11 H12 H12 H13 H13")
    r.validate_follow(hand, hand[:6], lead)
    with pytest.raises(RuleError):
        r.validate_follow(hand, hand[:4] + hand[-2:], lead)
    with pytest.raises(RuleError):
        r.validate_follow(hand, hand[:2] + hand[4:8], lead)


def test_follow_planner_avoids_consuming_later_tractor():
    r = Rules()
    hand = cards("H3 H3 H4 H4 H5 H5 H6 H6 H7 H7 H9 H9 H10 H10 H11 H11")
    score, witness = r.follow_plan(hand, [3, 3, 2])
    assert score == (3, 0, 0, 3, 0, 0, 2, 0)
    assert len(witness) == len({c.id for c in witness}) == 16


def test_follow_signature_matches_exhaustive_subsets():
    r = Rules(10, "S")
    hand = cards("S13 S13 S14 S14 H10 H10 C10 C10 S10 S10 J15 J15")
    for demands in ([2], [3], [2, 2], [3, 1]):
        n = sum(demands) * 2
        score, witness = r.follow_plan(hand, demands)
        best = max(r.follow_plan(list(subset), demands)[0] for subset in itertools.combinations(hand, n))
        assert score == best
        assert r.follow_plan(witness, demands)[0] == score


@pytest.mark.parametrize("lead,follow,win", [
    ("H14", "S3", 1),
    ("H14 H14", "S3 S4", 0),
    ("H14 H14", "S3 S3", 1),
    ("H8 H8 H9 H9", "S4 S4 S6 S6", 0),
    ("H8 H8 H9 H9", "S4 S4 S5 S5", 1),
    ("H8", "C14", 0),
    ("H10", "D10", 0),
    ("H5 H5 H8", "S3 S4 S5", 0),
    ("H5 H5 H8", "S3 S3 S4", 1),
])
def test_trick_winner(lead, follow, win):
    assert Rules(10, "S").winner([(0, cards(lead)), (1, cards(follow))]) == win


def test_throw_failure_and_success():
    r = Rules()
    proposed = cards("H4 H4 H12")
    assert r.failed_throw(proposed, [cards("H5 H5")]) == proposed[:2]
    assert r.failed_throw(proposed, [cards("H14")]) == proposed[2:]
    # Multiple vulnerable shapes: smallest card count first, then rank.
    assert r.failed_throw(proposed, [cards("H5 H5 H14")]) == proposed[2:]
    assert r.failed_throw(proposed, [cards("H5 H8 H11")]) is None
    assert r.failed_throw(cards("H13 H14"), [cards("S14 S14")]) is None


def test_failed_throw_changes_actual_lead_and_keeps_other_cards():
    g = Game()
    g.phase, g.trump = "playing", "S"
    g.hands = [cards("H4 H4 H12"), cards("H14"), cards("C5"), cards("D6")]
    g.act(0, "play", [c.id for c in g.hands[0]])
    assert len(g.trick[0][1]) == 1
    assert len(g.hands[0]) == 2
    assert "甩牌失败" in g.events[-1]


def test_throw_overruff_compares_largest_component():
    r = Rules()
    lead = cards("H3 H3 H14")
    a = cards("S5 S5 J16")
    b = cards("S6 S6 S3")
    assert r.winner([(0, lead), (1, a), (2, b)]) == 2


@pytest.mark.parametrize("spec,multiplier", [
    ("H3", 2), ("H3 H3", 4), ("H3 H3 H4 H4", 6),
    ("H3 H3 H4 H4 H5 H5", 8), ("H13 H14", 3), ("H3 H3 H14", 4),
])
def test_bottom_multipliers(spec, multiplier):
    assert Rules().bottom_multiplier(cards(spec)) == multiplier


def test_bid_counterbid_and_self_reinforcement():
    g = Game()
    g.phase = "dealing"
    g.hands = [cards("H2 H2 J16 J16"), cards("S2 S2 J15 J15"), cards("C2"), cards("D2")]
    g.act(0, "bid", [0])
    g.act(1, "bid", [0, 1])
    g.act(0, "bid", [2, 3])
    assert g.bid["value"] == 4 and g.trump is None
    g.finish_dealing()
    assert g.phase == "burying"


def test_first_dealer_is_final_bidder_and_bury_exactly_eight():
    g = Game(1)
    g.start()
    finish_ai_deal(g)
    assert g.dealer == g.bid["seat"]
    assert len(g.hands[g.dealer]) == 33
    with pytest.raises(RuleError, match="8"):
        g.act(g.turn, "bury", [c.id for c in g.hands[g.turn][:7]])
    action, ids = g.ai_action(g.turn)
    g.act(g.turn, action, ids)
    assert [len(h) for h in g.hands] == [25] * 4
    assert g.phase == "playing" and len(g.bottom) == 8


def test_invalid_actions_do_not_mutate_game():
    g = Game(2)
    g.start()
    before = g.view(0)
    for seat, action, ids in [(1, "pass", []), (0, "play", [1]), (0, "bid", [999]),
                              (0, "bid", [True]), (0, "bid", None), (0, "bid", [0, 0])]:
        with pytest.raises(RuleError):
            g.act(seat, action, ids)
        assert g.view(0) == before


@pytest.mark.parametrize("score,team,gain,next_dealer", [
    (0, 0, 3, 2), (5, 0, 2, 2), (35, 0, 2, 2), (40, 0, 1, 2), (75, 0, 1, 2),
    (80, 1, 0, 1), (115, 1, 0, 1), (120, 1, 1, 1), (160, 1, 2, 1), (240, 1, 4, 1),
])
def test_level_and_dealer_transitions(score, team, gain, next_dealer):
    g = Game()
    g.score = score
    g.finish(0, cards("H3"))
    assert g.result["team"] == team
    assert g.result["gain"] == gain
    assert g.result["next_dealer"] == next_dealer
    assert g.dealer == 0
    g.next_round()
    assert g.dealer == next_dealer
    assert g.level == 2 + gain


def test_bottom_score_added_and_a_must_be_defended():
    g = Game()
    g.bottom = cards("H5 H10 H13")
    g.score = 30
    g.finish(1, cards("H3 H3"))
    assert g.score == 130 and g.result["multiplier"] == 4
    g = Game()
    g.levels = [13, 2]
    g.finish(0, cards("H3"))
    assert g.levels[0] == 14 and g.phase == "round_end"
    g.next_round()
    g.finish(2, cards("H3"))
    assert g.phase == "match_end" and g.result["champion"] == 0


def test_opponent_hands_and_bottom_are_private():
    g = Game(1)
    g.start()
    view = g.view(0)
    assert {c["id"] for c in view["hand"]} == {c.id for c in g.hands[0]}
    assert "hands" not in view and view["bottom"] == []


def test_ai_wins_with_smallest_sufficient_card_without_reading_opponent_hands():
    g = Game()
    g.phase, g.trump, g.turn = "playing", "S", 1
    g.hands[1] = cards("H3 H11 H14")
    g.trick = [(0, cards("H10"))]
    action, ids = g.ai_action(1, "basic")
    assert action == "play" and ids == [1]
    g.hands[0] = cards("J16 J16")
    g.hands[2] = cards("S2 S2")
    g.hands[3] = cards("H12 H12")
    assert g.ai_action(1, "basic") == (action, ids)


def test_ai_overruffs_with_matching_stronger_pair():
    g = Game()
    g.phase, g.trump, g.turn = "playing", "S", 2
    g.hands[2] = cards("S3 S3 S8 S8")
    g.trick = [(0, cards("H5 H5")), (1, cards("S6 S6"))]
    assert g.ai_action(2) == ("play", [2, 3])


@pytest.mark.parametrize("seed", range(30))
def test_full_ai_matches_conserve_cards_and_complete(seed):
    g = Game(seed)
    g.start()
    for _ in range(200):
        actions = 0
        while g.phase not in ("round_end", "match_end"):
            if g.phase == "dealing":
                finish_ai_deal(g)
                continue
            action, ids = g.ai_action(g.turn)
            g.act(g.turn, action, ids)
            assert sum(map(len, g.hands)) + len(g.bottom) + sum(len(p["cards"]) for t in g.history for p in t["plays"]) + sum(len(cs) for _, cs in g.trick) == 108
            actions += 1
            assert actions < 200
        assert all(not h for h in g.hands)
        if g.phase == "match_end":
            return
        g.next_round()
    pytest.fail("Match did not complete within 200 rounds")
