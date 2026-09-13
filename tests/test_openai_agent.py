import asyncio
from contextlib import ExitStack
from dataclasses import replace
import json
import random
import threading
import time

import pytest

from levelup.ai import get_strategy, observe
from levelup.ai.candidates import generate_candidates
from levelup.ai import openai_agent as agent
from levelup.ai.rule_based import RuleBasedStrategy
from levelup.simulation import Simulation
from levelup import server
from tests.test_server import client, connect, receive, prepare_deal


def playing(**kwargs):
    sim = Simulation(32000, **kwargs)
    while sim.game.phase != 'playing':
        sim.step()
    return sim


@pytest.fixture
def position():
    with playing() as sim:
        for _ in range(80):
            sim.step()
            ctx = sim.observation(sim.game.turn)
            candidates = generate_candidates(ctx)
            if ctx.history and ctx.trick and len(candidates) > 1:
                return ctx, candidates
        pytest.fail('Expected a decision with history and multiple choices')


def envelope(value=None, *, status='completed', refusal=False):
    content = ({'type': 'refusal', 'refusal': 'Cannot answer'} if refusal else
               {'type': 'output_text', 'text': json.dumps(value), 'annotations': []})
    return {'id': 'resp_test', 'object': 'response', 'created_at': 0, 'status': status,
            'model': 'gpt-5-mini', 'output': [{'id': 'msg_test', 'type': 'message',
                'status': 'completed', 'role': 'assistant', 'content': [content]}],
            'usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}}


@pytest.fixture
def sdk(monkeypatch):
    """Exercise the official SDK's HTTP serialization and response parsing."""
    openai = pytest.importorskip('openai')
    import httpx
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key-not-a-secret')
    monkeypatch.delenv('LEVELUP_OPENAI_MODEL', raising=False)
    requests = []
    config = {'response': envelope({'candidate_id': 0, 'reason': '保留主牌，先兑现大牌。'}), 'status': 200}

    async def handler(request):
        requests.append(request)
        if 'wait' in config:
            await config['wait']()
        return httpx.Response(config['status'], json=config['response'], headers={'x-request-id': 'req_test'})

    def factory(seconds):
        return openai.AsyncOpenAI(api_key='test-key-not-a-secret', max_retries=0, timeout=seconds,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(agent, 'make_client', factory)
    return requests, config


def test_sdk_request_and_selection(position, sdk):
    ctx, candidates = position
    requests, config = sdk
    choices = tuple({tuple(sorted(c.key for c in a.cards)): a for a in candidates}.values())
    config['response'] = envelope({'candidate_id': len(choices) - 1, 'reason': '保留对子。'})
    result = asyncio.run(agent.OpenAIAgentStrategy().decide(ctx, candidates))
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert requests[0].url.path == '/v1/responses'
    assert payload['model'] == agent.DEFAULT_MODEL
    assert payload['store'] is False and payload['max_output_tokens'] == 2048
    assert payload['instructions'] == agent.RULES
    assert 'What cards do you want to play now?' in payload['input']
    assert 'previous_response_id' not in payload
    state = json.loads(payload['input'].split('\n', 1)[1])
    assert len(state['previous_tricks']) == len(ctx.history) > 0
    assert state['current_trick'] and len(state['hand']) == len(ctx.hand)
    assert state['partner'] == ctx.partner
    assert payload['text']['format']['schema']['properties']['candidate_id']['enum'] == list(range(len(choices)))
    assert result.report['status'] == 'selected' and result.report['request_id'] == 'req_test'
    assert result.report['usage']['total_tokens'] == 120
    assert result.report['model_output']['output_text'] == json.dumps({'candidate_id': len(choices) - 1, 'reason': '保留对子。'})
    assert result.report['model_output']['response_id'] == 'resp_test'
    assert result.report['model_output']['output'][0]['content'] == config['response']['output'][0]['content']
    assert {r.action.key for r in result.ranked} == {a.key for a in candidates}
    assert tuple(sorted(c.key for c in result.ranked[0].action.cards)) == tuple(sorted(c.key for c in choices[-1].cards))
    assert result.ranked[0].reasons[-1].label == '保留对子。'
    assert result.ranked[0].score > result.ranked[1].score


def test_prompt_is_independent_of_hidden_cards():
    with playing() as sim:
        seat = (sim.game.dealer + 1) % 4
        ctx = observe(sim.game, seat)
        candidates = generate_candidates(ctx)
        before = agent.prompt_state(ctx, candidates)
        assert 'known_bottom' not in before
        hidden = [c for s, hand in enumerate(sim.game.hands) if s != seat for c in hand] + sim.game.bottom
        random.Random(17).shuffle(hidden)
        for s in range(4):
            if s != seat:
                sim.game.hands[s], hidden = hidden[:25], hidden[25:]
        sim.game.bottom = hidden
        assert agent.prompt_state(observe(sim.game, seat), candidates) == before
        dealer = observe(sim.game, sim.game.dealer)
        assert agent.prompt_state(dealer, ())['known_bottom'] == [agent.face(c) for c in sim.game.bottom]


@pytest.mark.parametrize('value', [[], {}, {'candidate_id': -1, 'reason': 'x'},
    {'candidate_id': 10000, 'reason': 'x'}, {'candidate_id': True, 'reason': 'x'},
    {'candidate_id': 0.0, 'reason': 'x'}, {'candidate_id': 0, 'reason': ''},
    {'candidate_id': 0, 'reason': 4}, {'candidate_id': 0, 'reason': 'x', 'ids': [99]}])
def test_untrusted_choices_fall_back(position, sdk, value):
    ctx, candidates = position
    sdk[1]['response'] = envelope(value)
    result = asyncio.run(agent.OpenAIAgentStrategy().decide(ctx, candidates))
    assert result.report['status'] == 'fallback'
    assert result.ranked == RuleBasedStrategy().rank(ctx, candidates)


def test_malformed_output_is_logged_before_json_parsing(position, sdk):
    raw = '{"candidate_id": 0, "reason": "unfinished'
    sdk[1]['response']['output'][0]['content'][0]['text'] = raw
    result = asyncio.run(agent.OpenAIAgentStrategy().decide(*position))
    assert result.report['reason'] == 'invalid_response'
    assert result.report['model_output']['output_text'] == raw


@pytest.mark.parametrize('kind', ['refusal', 'incomplete', 'api_error'])
def test_api_failures_fall_back_without_retry(position, sdk, kind):
    ctx, candidates = position
    if kind == 'api_error':
        sdk[1].update(status=429, response={'error': {'message': 'private-error-body', 'type': 'rate_limit_error'}})
    else:
        sdk[1]['response'] = envelope({}, status='incomplete' if kind == 'incomplete' else 'completed',
                                     refusal=kind == 'refusal')
    result = asyncio.run(agent.OpenAIAgentStrategy().decide(ctx, candidates))
    assert len(sdk[0]) == 1
    assert result.report['status'] == 'fallback'
    assert 'private-error-body' not in json.dumps(result.report)
    if kind == 'api_error':
        assert 'model_output' not in result.report  # No model response was received.
    else:
        output = result.report['model_output']
        assert output['status'] == sdk[1]['response']['status']
        assert output['output'][0]['content'] == sdk[1]['response']['output'][0]['content']
    assert result.ranked == RuleBasedStrategy().rank(ctx, candidates)


def test_timeout_cancels_http_and_keeps_legal_fallback(position, sdk):
    ctx, candidates = position
    cancelled = []
    async def wait():
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)
    sdk[1]['wait'] = wait
    start = time.monotonic()
    result = asyncio.run(agent.OpenAIAgentStrategy(seconds=.1).decide(ctx, candidates))
    assert time.monotonic() - start < 1
    assert cancelled == [True]
    assert result.report['reason'] == 'timeout'
    assert result.ranked == RuleBasedStrategy().rank(ctx, candidates)


def test_cancellation_propagates(position, sdk):
    async def scenario():
        entered = asyncio.Event()
        async def wait():
            entered.set()
            await asyncio.sleep(10)
        sdk[1]['wait'] = wait
        task = asyncio.create_task(agent.OpenAIAgentStrategy().decide(*position))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())


def test_local_paths_never_contact_api(position, monkeypatch):
    ctx, candidates = position
    def forbidden(*args):
        pytest.fail('No network call expected')
    monkeypatch.setattr(agent, 'make_client', forbidden)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    strategy = agent.OpenAIAgentStrategy()
    result = asyncio.run(strategy.decide(ctx, candidates))
    assert result.report['reason'] == 'missing_api_key'
    assert strategy.rank(replace(ctx, phase='burying'), candidates) == RuleBasedStrategy().rank(replace(ctx, phase='burying'), candidates)
    monkeypatch.setenv('OPENAI_API_KEY', 'test')
    assert asyncio.run(strategy.decide(ctx, candidates, seconds=0)).report['reason'] == 'no_time'
    assert asyncio.run(strategy.decide(ctx, (candidates[0],))).report['status'] == 'forced'
    assert strategy.rank_external(ctx, candidates).agent_report['status'] == 'external'


def test_missing_optional_sdk(position, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'test')
    def missing(*args):
        raise ImportError('openai')
    monkeypatch.setattr(agent, 'make_client', missing)
    assert asyncio.run(agent.OpenAIAgentStrategy().decide(*position)).report['reason'] == 'sdk_not_installed'


def test_configured_model_and_registry(position, sdk, monkeypatch):
    monkeypatch.setenv('LEVELUP_OPENAI_MODEL', 'my-compatible-model')
    ranked = get_strategy('openai_agent').rank(*position)
    assert ranked.agent_report['model'] == 'my-compatible-model'
    assert json.loads(sdk[0][0].content)['model'] == 'my-compatible-model'
    assert 'reasoning' not in json.loads(sdk[0][0].content)


def test_simulation_logs_decision_once_and_external_without_call(tmp_path, sdk):
    path = tmp_path / 'training.jsonl'
    with playing(training=True, log_path=path) as sim:
        sim.strategies = ('openai_agent',) * 4
        sim.step()
        assert len(sdk[0]) == 1
        row = next(r for r in map(json.loads, path.read_text().splitlines()) if r.get('agent', {}).get('status') == 'selected')
        assert row['agent']['usage']['total_tokens'] == 120
        assert json.loads(row['agent']['model_output']['output_text'])['candidate_id'] == 0
        assert row['chosen']['ids'] == row['agent']['chosen_ids']
        action, ids = sim.game.ai_action(sim.game.turn, 'rule_based')
        sim.command(sim.game.turn, action, ids)
        assert len(sdk[0]) == 1
        rows = list(map(json.loads, path.read_text().splitlines()))
        assert any(r.get('agent', {}).get('status') == 'external' for r in rows)


def test_agent_hint_leaves_websocket_responsive_and_manual_play_cancels(client, sdk):
    entered, cancelled = threading.Event(), threading.Event()
    async def wait():
        entered.set()
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()
    sdk[1]['wait'] = wait
    info = client.post('/api/rooms', json={'name': '南', 'ai_strategy': 'openai_agent'}).json()
    room = server.rooms[info['room']]
    with ExitStack() as stack:
        ws, _, state = connect(client, stack, info['room'], info['token'])
        ws.send_json({'action': 'start', 'version': state['version']})
        receive(ws, 'state', state['version'] + 1)
        version = prepare_deal(client, room, playing=True)
        receive(ws, 'state', version)
        ws.send_json({'action': 'hint', 'version': version})
        receive(ws, 'hint_pending')
        assert entered.wait(2)
        ws.send_json({'action': 'ping'})
        assert receive(ws, 'pong')['type'] == 'pong'
        action, ids = room.game.ai_action(0, 'rule_based')
        ws.send_json({'action': action, 'ids': ids, 'version': version})
        assert receive(ws, 'state', version + 1)['turn'] == 1
        assert cancelled.wait(2)


def test_agent_hint_returns_metadata(client, sdk):
    info = client.post('/api/rooms', json={'name': '南', 'ai_strategy': 'openai_agent'}).json()
    room = server.rooms[info['room']]
    with ExitStack() as stack:
        ws, _, state = connect(client, stack, info['room'], info['token'])
        ws.send_json({'action': 'start', 'version': state['version']})
        receive(ws, 'state', state['version'] + 1)
        version = prepare_deal(client, room, playing=True)
        receive(ws, 'state', version)
        ws.send_json({'action': 'hint', 'version': version})
        result = receive(ws, 'hint')
        assert result['agent']['status'] == 'selected'
        assert json.loads(result['agent']['model_output']['output_text'])['candidate_id'] == 0
        assert result['strategy'] == 'openai_agent' and result['version'] == version
        assert set(result['ids']) <= {c.id for c in room.game.hands[0]}


def test_room_actor_plays_selected_candidate_and_stops_thinking(monkeypatch, sdk):
    sdk[1]['response'] = envelope({'candidate_id': 0, 'reason': '私下分析牌势。', 'reaction': '先拿一墩'})
    monkeypatch.setattr(server, 'ROOM_TICK', .005)
    monkeypatch.setattr(server, 'AI_DELAY', 0)
    class Socket:
        async def send_json(self, state):
            pass
    async def scenario():
        with playing() as sim:
            game = sim.game
            seat, version = game.turn, game.version
            room = server.Room('AGENTX', game=game, ai_strategy='openai_agent',
                players={i: server.Player(str(i), socket=Socket(), auto=(i == seat)) for i in range(4)})
            server.rooms[room.code] = room
            task = asyncio.create_task(server.run_room(room))
            try:
                async with asyncio.timeout(3):
                    while game.version == version:
                        await asyncio.sleep(.01)
                assert game.turn == (seat + 1) % 4
                assert len(sdk[0]) == 1 and room.search_task is None
                assert game.trick[0][0] == seat
                for viewer in range(4):
                    state = room.view(viewer)
                    assert state['trick'][0]['reaction'] == '先拿一墩'
                    assert '私下分析牌势' not in json.dumps(state, ensure_ascii=False)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                server.rooms.pop(room.code, None)
    asyncio.run(scenario())


def test_full_agent_round_is_playable_through_sdk(sdk):
    with Simulation(32002, strategies=('openai_agent',) * 4) as sim:
        sim.run_round()
        assert sim.game.phase == 'round_end'
        assert all(not hand for hand in sim.game.hands)
        assert sdk[0]  # Mock transport; this test never spends API credits.


@pytest.mark.parametrize('reaction, expected', [('', ''), (None, ''), (12, ''),
    ('这分我收了', '这分我收了'), ('一二三四五六七八九', '一二三四五六七八九'),
    ('一二三四五六七八九十', ''), ('拿分\n走人', '')])
def test_optional_reaction_does_not_break_play(position, sdk, reaction, expected):
    sdk[1]['response'] = envelope({'candidate_id': 0, 'reason': '保留对子。', 'reaction': reaction})
    result = asyncio.run(agent.OpenAIAgentStrategy().decide(*position))
    assert result.report['status'] == 'selected'
    assert result.report['reaction'] == expected
    payload = json.loads(sdk[0][0].content)
    assert payload['text']['format']['schema']['properties']['reaction']['maxLength'] == 9
    assert 'Everyone at the' in payload['instructions']


@pytest.mark.parametrize('reaction_index', [0, 3])
def test_reaction_moves_to_recap_and_never_attaches_to_next_deal(reaction_index):
    with playing() as sim:
        room = server.Room('REACTX', game=sim.game)
        first = sim.game.turn
        for i in range(4):
            seat = sim.game.turn
            sim.step()
            room.remember_reaction(seat, 1, '先拿一墩' if i == reaction_index else '')
        view = room.view(first)
        assert view['last_trick']['plays'][reaction_index]['reaction'] == '先拿一墩'
        assert all('reaction' not in p for i, p in enumerate(view['last_trick']['plays']) if i != reaction_index)
        assert 'reaction' not in sim.game.last_trick['plays'][reaction_index]  # No engine/history mutation.
        sim.step()
        assert all('reaction' not in p for p in room.view(first)['trick'])
        assert room.view(first)['last_trick']['plays'][reaction_index]['reaction'] == '先拿一墩'
        sim.game.deal_id += 1
        assert 'reaction' not in room.view(first)['last_trick']['plays'][reaction_index]
