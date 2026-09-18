"""Incremental dealing, simultaneous declarations and a deterministic room clock."""
import pytest

from levelup.ai import observe
from levelup.game import Game, RuleError, deck
from levelup.server import Player, Room
from levelup import server


def ordered_game():
    game = Game(5)
    game.start()
    # First two seats get S2/H2; second lap gives each its matching copy.
    ids = [0, 13, 2, 3, 54, 67, 6, 7]
    cards = deck()
    cards = [cards[i] for i in ids] + [c for c in cards if c.id not in ids]
    game.draw_pile, game.bottom = cards[:100], cards[100:]
    return game


def human_room(game):
    return Room("CLOCK1", game=game,
                players={i: Player(str(i), socket=object()) for i in range(4)})


def test_draw_order_conservation_privacy_and_completion():
    game = ordered_game()
    for count in range(100):
        with pytest.raises(RuleError):
            game.finish_dealing()
        assert game.draw_card() == count % 4
        assert game.dealt == count + 1
        all_cards = sum(game.hands, []) + game.draw_pile + game.bottom
        assert len(all_cards) == len({c.id for c in all_cards}) == 108
        for seat in range(4):
            view = game.view(seat)
            assert {c['id'] for c in view['hand']} == {c.id for c in game.hands[seat]}
            assert not view['bottom'] and 'draw_pile' not in view and 'hands' not in view
            assert not observe(game, seat).known_bottom
    assert [len(h) for h in game.hands] == [25] * 4
    with pytest.raises(RuleError):
        game.draw_card()


def test_bid_anytime_does_not_advance_draw_and_undrawn_cards_are_rejected():
    game = ordered_game()
    with pytest.raises(RuleError, match="不在"):
        game.act(0, "bid", [0])
    game.draw_card()
    version = game.version
    game.act(0, "bid", [0])  # Draw pointer is already at seat 1.
    assert (game.turn, game.dealt, game.version) == (1, 1, version + 1)
    with pytest.raises(RuleError, match="不在"):
        game.act(0, "bid", [0, 54])
    for _ in range(5):
        game.draw_card()
    game.act(1, "bid", [13, 67])
    assert game.trump == "H" and game.bid['seat'] == 1
    assert (game.turn, game.dealt) == (2, 6)
    with pytest.raises(RuleError, match="更强"):
        game.act(0, "bid", [0, 54])


def test_self_reinforcement_during_deal():
    game = ordered_game()
    game.draw_card()
    game.act(0, "bid", [0])
    for _ in range(4):
        game.draw_card()
    game.act(0, "bid", [0, 54])
    assert game.bid['value'] == 2 and game.dealt == 5


def test_no_bid_redeal_or_bottom_fallback():
    game = ordered_game()
    old_id = game.deal_id
    for _ in range(100):
        game.draw_card()
    game.finish_dealing()
    assert game.phase == 'dealing' and game.dealt == 0
    assert game.deal_id != old_id and not any(game.hands)
    game.round = 2
    game.dealer = 2
    for _ in range(100):
        game.draw_card()
    expected = next(c.suit for c in game.bottom if c.suit != 'J')
    game.finish_dealing()
    assert game.phase == 'burying' and game.trump == expected
    assert len(game.hands[2]) == 33


def test_clock_half_seconds_bid_does_not_reset_and_resume_has_no_burst():
    room = human_room(ordered_game())
    room.sync_deal_clock(10)
    assert not room.advance_dealing(10.499)
    assert room.advance_dealing(10.5) and room.game.dealt == 1
    room.game.act(0, 'bid', [0])
    room.sync_deal_clock(10.6)
    assert room.next_draw_at == 11
    assert not room.advance_dealing(10.999)
    assert room.advance_dealing(11) and room.game.dealt == 2
    room.sync_deal_clock(90, resume=True)
    assert not room.advance_dealing(90.49)
    assert room.advance_dealing(90.5) and room.game.dealt == 3
    assert room.advance_dealing(100) and room.game.dealt == 4


def test_last_card_grace_window_then_settlement_once():
    game = ordered_game()
    room = human_room(game)
    room.sync_deal_clock(0)
    for n in range(1, 101):
        assert room.advance_dealing(n * .5)
    assert game.phase == 'dealing' and room.deal_closes_at == 110
    assert not room.advance_dealing(109.999)
    game.act(1, 'bid', [13])  # Seat 0 can still counter with its pair.
    assert room.advance_dealing(110)
    assert game.bid_passed == {1, 2, 3}
    assert room.deal_closes_at == 170
    assert room.advance_dealing(170)
    assert game.phase == 'burying' and game.dealer == 1
    assert [len(h) for h in game.hands] == [25, 33, 25, 25]
    assert not room.advance_dealing(171)
    with pytest.raises(RuleError):
        game.act(1, 'bid', [13, 67])


@pytest.mark.parametrize('strategy', ['basic', 'rule_based'])
def test_ai_bids_mid_deal_without_future_information(strategy, monkeypatch):
    monkeypatch.setattr(server, 'AI_BID_DELAY', 0)
    game = ordered_game()
    room = Room('AICLK1', game=game, ai_strategy=strategy)
    room.sync_deal_clock(0)
    assert game.ai_action(0, strategy)[0] == 'pass'
    room.advance_dealing(.5)
    snapshot = observe(game, 0)
    ranking = game.ai_rankings(0, strategy)
    game.draw_pile.reverse()
    game.bottom.reverse()
    assert observe(game, 0) == snapshot
    assert game.ai_rankings(0, strategy) == ranking
    room.advance_dealing(.501)
    assert game.bid and game.bid['seat'] == 0 and game.dealt == 1


def test_last_draw_ai_can_bid_before_close(monkeypatch):
    monkeypatch.setattr(server, 'AI_BID_DELAY', .7)
    game = ordered_game()
    # Arrange the only level card held by seat 3 as its final draw.
    cards = deck()
    ordinary = [c for c in cards if c.rank != 2 and c.suit != 'J']
    game.hands = [[], [], [], ordinary[:24]]
    game.draw_pile = [cards[13]]
    game.turn, game.dealt = 3, 99
    room = human_room(game)
    del room.players[3]
    room.sync_deal_clock(0)
    room.advance_dealing(.5)
    assert not game.bid and game.dealt == 100
    room.advance_dealing(1.21)
    assert game.bid and game.bid['seat'] == 3
    # The declarer and everyone unable to counter skip confirmation immediately.
    assert game.phase == 'burying'


def test_pass_only_after_draws_and_new_bid_resets_confirmations():
    game = ordered_game()
    with pytest.raises(RuleError, match="摸牌完成"):
        game.act(0, "pass")
    while game.draw_pile:
        game.draw_card()
    for seat in (0, 2, 3):
        game.act(seat, "pass")
    assert game.view(0)["bid_passed"] == [0, 2, 3]
    game.act(1, "bid", [13])
    assert not game.bid_passed and game.view(0)["bid_revision"] == 1
    game.act(0, "pass")
    game.act(1, "bid", [13, 67])
    assert not game.bid_passed


def test_all_humans_pass_close_early_and_no_bid_redeal_clears_passes():
    room = human_room(ordered_game())
    game = room.game
    while game.draw_pile:
        game.draw_card()
    room.sync_deal_clock(100)
    for seat in range(4):
        game.act(seat, "pass")
    assert room.advance_dealing(100.1)
    assert game.phase == "dealing" and game.dealt == 0  # No first-round declaration.
    assert not game.bid_passed and room.deal_closes_at is None
    while game.draw_pile:
        game.draw_card()
    # Find a legal declaration in the newly shuffled hands.
    for seat, hand in enumerate(game.hands):
        level_card = next((c for c in hand if c.rank == game.level), None)
        if level_card:
            game.act(seat, "bid", [level_card.id])
            break
    room.sync_deal_clock(101)
    for seat in range(4):
        game.act(seat, "pass")
    assert room.advance_dealing(101.1)
    assert game.phase == "burying"  # No need to wait until 161.


def test_auto_confirms_declarer_and_ineligible_ai_but_waits_for_eligible_human(monkeypatch):
    game = ordered_game()
    room = human_room(game)
    for seat in (0, 2, 3):
        del room.players[seat]
    monkeypatch.setattr(game, "ai_action", lambda *_: ("pass", []))
    room.sync_deal_clock(0)
    for i in range(1, 100):
        room.advance_dealing(i * .5)
    game.act(0, "bid", [0])
    room.advance_dealing(50)
    assert game.bid_passed == {0, 2, 3}
    room.advance_dealing(50.71)
    assert game.bid_passed == {0, 2, 3} and game.phase == "dealing"
    game.act(1, "pass")
    room.advance_dealing(50.72)
    assert game.phase == "burying"


@pytest.mark.parametrize("round_number,dealer", [(1, 1), (2, 0)])
@pytest.mark.parametrize("confirm", [True, False])
def test_bottom_waits_for_eligible_player_confirmation_or_full_minute(round_number, dealer, confirm):
    game = ordered_game()
    game.round = round_number
    if round_number == 2:
        game.dealer = dealer
    while game.draw_pile:
        game.draw_card()
    game.act(1, "bid", [13])
    room = human_room(game)
    room.sync_deal_clock(100)
    bottom_ids = {c.id for c in game.bottom}
    for seat in range(4):
        if seat != 0:  # Seat 0 can counter with S2/S2, including as next-round dealer.
            game.act(seat, "pass")
    assert room.deal_closes_at == 160
    for now in (115, 130, 159.999):
        assert not room.advance_dealing(now)
        assert game.phase == "dealing"
        assert [len(h) for h in game.hands] == [25] * 4
        assert {c.id for c in game.bottom} == bottom_ids
        assert not bottom_ids.intersection(c["id"] for c in room.view(dealer)["hand"])
        assert room.view(dealer)["bottom"] == []
    if confirm:
        game.act(0, "pass")
        assert room.advance_dealing(159.999)
    else:
        assert room.advance_dealing(160)
    assert game.phase == "burying" and game.dealer == dealer
    assert len(game.hands[dealer]) == 33
    assert bottom_ids.issubset(c.id for c in game.hands[dealer])


@pytest.mark.parametrize("bid_ids,hand_ids,expected", [
    ([], [13], None),                       # A single can open bidding.
    ([], [1, 52], "no_legal_bid"),          # Ordinary card and unmatched joker.
    ([0], [13], "no_legal_bid"),            # Single cannot counter single.
    ([0], [13, 67], None),                  # Pair can counter single.
    ([0, 54], [13, 67], "no_legal_bid"),     # Equal-strength pairs cannot counter.
    ([0, 54], [52, 106], None),             # Small joker pair beats level pair.
    ([52, 106], [13, 67], "no_legal_bid"),
    ([52, 106], [53, 107], None),           # Big joker pair beats small joker pair.
    ([53, 107], [52, 106], "no_legal_bid"),
])
def test_final_confirmation_checks_actual_legal_bids(bid_ids, hand_ids, expected):
    game = ordered_game()
    cards = deck()
    game.draw_pile = []
    game.hands = [[cards[i] for i in bid_ids], [cards[i] for i in hand_ids], [], []]
    if bid_ids:
        game.act(0, "bid", bid_ids)
    version = game.version
    assert game.bid_skip_reason(1) == expected
    assert game.view(1)["bid_skip_reason"] == expected
    assert game.version == version  # Computing the private prompt is read-only.


def test_current_declarer_skips_even_with_reinforcement_but_not_while_drawing():
    game = ordered_game()
    for _ in range(5):
        game.draw_card()
    game.act(0, "bid", [0])
    room = human_room(game)
    assert game.bid_skip_reason(0) is None
    assert not room.auto_confirm_bids() and not game.bid_passed
    while game.draw_pile:
        game.draw_card()
    game.validate_bid(0, [c for c in game.hands[0] if c.id in (0, 54)])
    assert game.bid_skip_reason(0) == "own_bid"
    assert room.advance_dealing(100)
    assert game.bid_passed == {0, 2, 3} and game.phase == "dealing"
    # Same-strength pair cannot counter: no unnecessary timer after this bid.
    game.act(1, "bid", [13, 67])
    assert room.advance_dealing(101)
    assert game.phase == "burying" and game.dealer == 1


def test_counterbid_resets_manual_confirmation_and_keeps_stronger_options_open():
    game = ordered_game()
    cards = deck()
    game.draw_pile = []
    game.hands = [[cards[0]], [cards[13], cards[67]],
                  [cards[52], cards[106]], [cards[1]]]
    game.act(0, "bid", [0])
    room = human_room(game)
    room.advance_dealing(100)
    assert game.bid_passed == {0, 3}
    game.act(2, "pass")
    game.act(1, "bid", [13, 67])
    room.advance_dealing(110)
    assert game.bid_passed == {0, 1, 3}  # Seat 2 gets a fresh choice with its jokers.
    assert room.deal_closes_at == 170 and game.phase == "dealing"
    assert game.bid_skip_reason(2) is None
    game.act(2, "bid", [52, 106])
    room.advance_dealing(111)
    assert game.phase == "burying" and game.dealer == 2


def test_final_confirmation_uses_current_level():
    game = ordered_game()
    game.level = 7
    cards = deck()
    game.draw_pile = []
    game.hands = [[cards[5]], [cards[18], cards[72]], [cards[13], cards[67]], []]
    game.act(0, "bid", [5])
    assert game.bid_skip_reason(1) is None  # H7 pair.
    assert game.bid_skip_reason(2) == "no_legal_bid"  # H2 pair is irrelevant.
