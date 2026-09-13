"""Clock-free incremental dealing for tests and reproducible AI comparisons."""


def finish_ai_deal(game, strategies=("rule_based",) * 4, command=None, choose=None):
    """Draw one card at a time and let all AIs react using only current hands.

    Production pacing and reaction delay belong to Room, not this simulator.
    An optional command(seat, action, ids) callback records every transition.
    choose(seat) can select and record a self-play decision, including waits.
    """
    def apply(seat, action, ids=None):
        if command is not None:
            return command(seat, action, ids)
        if action == "draw":
            return game.draw_card()
        if action == "finish_dealing":
            return game.finish_dealing()
        return game.act(seat, action, ids)

    attempts = 0
    seen = {}
    while game.phase == "dealing":
        if game.draw_pile:
            apply(None, "draw")
        # A stronger counterbid may give an earlier seat another legal reply.
        for _ in range(5):
            declared = False
            for seat in range(4):
                signature = (game.deal_id, len(game.hands[seat]),
                             game.bid["seat"] if game.bid else None,
                             game.bid["value"] if game.bid else 0)
                if seen.get(seat) == signature:
                    continue
                seen[seat] = signature
                action, ids = choose(seat) if choose is not None else game.ai_action(seat, strategies[seat])
                if action == "bid":
                    apply(seat, action, ids)
                    declared = True
            if not declared:
                break
        if not game.draw_pile:
            apply(None, "finish_dealing")
            attempts += 1
            if attempts > 50:
                raise RuntimeError("AI simulation repeatedly failed to declare")
