import asyncio
from contextlib import ExitStack
from dataclasses import replace
import random
import threading
import time

import pytest

from levelup.ai import observe, get_strategy
from levelup.ai.belief import DealSampler
from levelup.ai.candidates import generate_candidates
from levelup.ai.search import SearchStrategy, SearchResult, evaluate_world, shutdown_search
from levelup.ai.rule_based import RuleBasedStrategy
from levelup.game import Card, Game, deck
from levelup.simulation import Simulation
from levelup import server
from tests.test_server import client, connect, create, receive, prepare_deal


def playing(seed=32000):
    sim = Simulation(seed)
    while sim.game.phase != 'playing':
        sim.step()
    return sim


@pytest.mark.parametrize('seed', [32000, 32001, 32002])
def test_samples_preserve_information_and_replay_obligations(seed):
    with playing(seed) as sim:
        for turn in range(45):
            if sim.game.phase != 'playing':
                break
            if turn % 11 == 0:
                ctx = observe(sim.game, sim.game.turn)
                sampler = DealSampler(ctx)
                sample = sampler.sample(random.Random(seed+turn))
                assert sample is not None
                assert observe(sample, ctx.seat) == ctx
                assert tuple(map(len, sample.hands)) == ctx.counts
                assert len(sample.bottom) == 8
                all_cards = [c for h in sample.hands for c in h] + sample.bottom
                all_cards += [c for trick in (*ctx.history, ctx.trick) for p in trick for c in p.cards]
                assert len(all_cards) == len({c.id for c in all_cards}) == 108
                assert sampler.consistent_history(sample.hands)
            sim.step()


def test_sampler_does_not_read_real_hidden_hands():
    with playing() as sim:
        seat = (sim.game.dealer + 1) % 4
        ctx = observe(sim.game, seat)
        a = DealSampler(ctx).sample(random.Random(9))
        hidden = [c for s, h in enumerate(sim.game.hands) if s != seat for c in h] + sim.game.bottom
        random.Random(11).shuffle(hidden)
        for s in range(4):
            if s != seat:
                sim.game.hands[s], hidden = hidden[:25], hidden[25:]
        sim.game.bottom = hidden
        assert observe(sim.game, seat) == ctx
        b = DealSampler(observe(sim.game, seat)).sample(random.Random(9))
        assert a.hands == b.hands and a.bottom == b.bottom


def test_pair_obligation_filters_impossible_sample():
    from levelup.ai.interface import AIContext, Play
    lead = (Card(0, 'H', 8), Card(54, 'H', 8))
    follow = (Card(1, 'H', 3), Card(2, 'H', 4))
    ctx = AIContext(0, 'playing', (), 2, 'S', 0, 0, (0, 0, 0, 0),
                    trick=(Play(0, lead), Play(1, follow)))
    sampler = object.__new__(DealSampler)
    sampler.ctx = ctx
    # Player 1 could not have held an H9 pair when following the observed pair with singles.
    assert not sampler.consistent_history([[], [Card(3, 'H', 9), Card(57, 'H', 9)], [], []])
    assert sampler.consistent_history([[], [Card(3, 'C', 9), Card(57, 'C', 9)], [], []])


def test_exposed_dealer_cards_may_have_been_buried():
    from levelup.ai.interface import Bid
    with playing() as sim:
        seat = (sim.game.dealer + 1) % 4
        ctx = observe(sim.game, seat)
        hidden = next(c for c in sim.game.hands[sim.game.dealer] if c.id not in {x.id for x in ctx.hand})
        ctx = replace(ctx, declarations=(Bid(ctx.dealer, 1, (hidden,)),))
        sampler = DealSampler(ctx)
        index = next(i for i,c in enumerate(sampler.cards) if c.id == hidden.id)
        assert sampler.allowed[index] == {ctx.dealer, 4}


def test_search_zero_budget_fallback_and_nonplaying_fallback():
    with playing() as sim:
        ctx = sim.observation(sim.game.turn)
        candidates = generate_candidates(ctx)
        assert SearchStrategy(0).rank(ctx,candidates) == RuleBasedStrategy().rank(ctx,candidates)
    game = Game(1); game.start()
    ctx = observe(game,0); candidates = generate_candidates(ctx)
    assert get_strategy('search').rank(ctx,candidates) == RuleBasedStrategy().rank(ctx,candidates)


def test_world_evaluation_is_repeatable_and_deadline_aware():
    with playing() as sim:
        for _ in range(30): sim.step()
        ctx = sim.observation(sim.game.turn)
        actions = list(enumerate(generate_candidates(ctx)[:2]))
        a = evaluate_world(ctx, actions, 900, 0, time.monotonic()+10)
        b = evaluate_world(ctx, actions, 900, 0, time.monotonic()+10)
        assert a == b and a['values'] is not None
        assert evaluate_world(ctx,actions,900,0,time.monotonic()-1)['values'] is None


def test_process_search_returns_legal_ranking_within_budget():
    with playing() as sim:
        for _ in range(30): sim.step()
        ctx = sim.observation(sim.game.turn)
        actions = generate_candidates(ctx)
        start = time.monotonic()
        result = SearchStrategy(seconds=1, max_worlds=8).search(ctx,actions)
        assert time.monotonic()-start < 2.5
        assert {r.action.key for r in result.ranked} == {a.key for a in actions}
        move = result.ranked[0].action
        sim.game.act(ctx.seat,move.kind,[c.id for c in move.cards])
    shutdown_search()


def test_search_hint_does_not_block_ping_or_manual_play(client, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def slow_search(self, ctx, candidates, **kwargs):
        entered.set(); release.wait(3)
        return SearchResult(RuleBasedStrategy().rank(ctx,candidates), {})
    monkeypatch.setattr(SearchStrategy,'search',slow_search)
    info=client.post('/api/rooms',json={'name':'南','ai_strategy':'search'}).json()
    room=server.rooms[info['room']]
    try:
        with ExitStack() as stack:
            host,_,state=connect(client,stack,info['room'],info['token'])
            host.send_json({'action':'start','version':state['version']})
            receive(host,'state',state['version']+1)
            version=prepare_deal(client,room,playing=True)
            receive(host,'state',version)
            host.send_json({'action':'hint','version':version})
            assert entered.wait(2)
            host.send_json({'action':'ping'})
            assert receive(host,'pong')['type']=='pong'
            action,ids=room.game.ai_action(0,'rule_based')
            host.send_json({'action':action,'ids':ids,'version':version})
            assert receive(host,'state',version+1)['turn']==1
            release.set()
            host.send_json({'action':'ping'})
            assert receive(host,'pong')['type']=='pong'
    finally:
        release.set()


def test_actor_discards_stale_search_and_keeps_room_lock_available(monkeypatch):
    monkeypatch.setattr(server,'ROOM_TICK',.005)
    monkeypatch.setattr(server,'AI_DELAY',0)
    entered,release=threading.Event(),threading.Event()
    def slow_search(self,ctx,candidates,**kwargs):
        entered.set(); release.wait(3)
        return SearchResult(RuleBasedStrategy().rank(ctx,candidates),{})
    monkeypatch.setattr(SearchStrategy,'search',slow_search)
    class Socket:
        async def send_json(self,state): pass
    async def scenario():
        with playing() as sim:
            game=sim.game; seat=game.turn
            room=server.Room('SEARCHX',game=game,ai_strategy='search',
                players={i:server.Player(str(i),socket=Socket(),auto=(i==seat)) for i in range(4)})
            server.rooms[room.code]=room
            task=asyncio.create_task(server.run_room(room))
            try:
                for _ in range(100):
                    if entered.is_set(): break
                    await asyncio.sleep(.005)
                assert entered.is_set()
                async with asyncio.timeout(.2):
                    async with room.lock:
                        room.players[seat].auto=False
                        game.version+=1
                        room.mark()
                        version=game.version
                release.set()
                await asyncio.sleep(.05)
                assert game.version==version and game.turn==seat
                assert room.search_task is None
            finally:
                release.set(); task.cancel()
                await asyncio.gather(task,return_exceptions=True)
                server.rooms.pop(room.code,None)
    asyncio.run(scenario())


def test_search_wrapper_enforces_deadline_and_signals_cancellation(monkeypatch):
    release = threading.Event()
    def slow(self, ctx, candidates, **kwargs):
        release.wait(2)
        return SearchResult([], {})
    monkeypatch.setattr(SearchStrategy, 'search', slow)
    async def scenario():
        with playing() as sim:
            ctx = sim.observation(sim.game.turn)
            cancel = threading.Event()
            start = time.monotonic()
            try:
                with pytest.raises(asyncio.TimeoutError):
                    await server.search_ranking(ctx, SearchStrategy(), .05, cancel)
                assert cancel.is_set() and time.monotonic() - start < .6
            finally:
                release.set()
    asyncio.run(scenario())


def test_search_budget_respects_short_room_timer():
    room = server.Room('SHORT', turn_seconds=15)
    room.game.phase = 'playing'
    room.changed = time.monotonic() - 4
    assert 10 < room.search_budget() <= 10.5
    room.changed = time.monotonic() - 16
    assert room.search_budget() == 0
    room.turn_seconds = None
    assert room.search_budget() == 28


def test_sampled_endgame_solver_matches_exhaustive_round_utility():
    from levelup.ai.search import Endgame, utility, clone
    from levelup.god_teacher import exhaustive_actions
    from tests.test_play_model import small_game
    game = small_game()
    def brute(g, team):
        if g.phase in ('round_end', 'match_end'):
            return utility(g, team)
        values = []
        for action in exhaustive_actions(g):
            child = clone(g)
            child.act(child.turn, action.kind, [c.id for c in action.cards])
            values.append(brute(child, team))
        return (max if g.turn % 2 == team else min)(values)
    for team in (0, 1):
        solver = Endgame(team, time.monotonic()+5)
        assert solver.value(game) == brute(game, team)
        other = clone(game); other.turn = 1
        assert solver.value(other) == brute(other, team)


def test_training_logs_search_statistics_without_rescoring(tmp_path):
    import json
    from levelup.ai import STRATEGIES
    prior = STRATEGIES['search']
    STRATEGIES['search'] = SearchStrategy(seconds=0)
    try:
        path = tmp_path/'search.jsonl'
        with Simulation(32009, strategies=('search',)*4, log_path=path, training=True) as sim:
            while sim.game.phase != 'playing': sim.step()
            sim.step()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        decisions = [r for r in rows if r['event']=='decision' and r['observation']['phase']=='playing']
        assert decisions and decisions[0]['search']['budget_seconds'] == 0
        assert decisions[0]['search']['reason'] in ('no_time', 'single_candidate')
        assert len(decisions[0]['privileged']['hands']) == 4
    finally:
        STRATEGIES['search'] = prior


@pytest.mark.parametrize('level', [5, 10, 14])
def test_belief_sampling_supports_later_levels(level):
    from levelup.ai.simulation import finish_ai_deal
    game = Game(45000+level)
    game.levels = [level, level]
    game.start(); finish_ai_deal(game)
    for _ in range(13):
        action, ids = game.ai_action(game.turn, 'rule_based')
        game.act(game.turn, action, ids)
    ctx = observe(game, game.turn)
    assert ctx.level == level
    sampler = DealSampler(ctx)
    sample = sampler.sample(random.Random(level))
    assert sample is not None and observe(sample, ctx.seat) == ctx
    assert sampler.consistent_history(sample.hands)
