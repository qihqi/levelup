import asyncio
import math
from contextlib import ExitStack
import time

import pytest

from levelup import server
from levelup.ai.simulation import finish_ai_deal
from levelup.game import Game
from tests.test_server import client, connect, create, receive, prepare_deal


def test_timer_host_permissions_validation_and_round_lock(client):
    info = create(client)
    room = server.rooms[info['room']]
    with ExitStack() as stack:
        host, _, _ = connect(client, stack, info['room'], info['token'])
        guest, _, state = connect(client, stack, info['room'])
        assert state['turn_seconds'] == 60 and state['bury_seconds'] == 90
        guest.send_json({'action':'timer', 'seconds':None, 'version':state['version']})
        assert '房主' in receive(guest,'error')['message']
        for invalid in (True, '60', 0, -1, 600, 30.0, [], {}):
            host.send_json({'action':'timer', 'seconds':invalid, 'version':room.game.version})
            assert '请选择' in receive(host,'error')['message']
        host.send_json({'action':'timer', 'version':room.game.version})
        assert '请选择' in receive(host,'error')['message']
        for seconds in (15, 30, 60, 120, None):
            version = room.game.version
            host.send_json({'action':'timer','seconds':seconds,'version':version})
            state = receive(host,'state',version+1)
            shared = receive(guest,'state',state['version'])
            assert state['turn_seconds'] == shared['turn_seconds'] == seconds
            assert state['bury_seconds'] == (None if seconds is None else math.ceil(seconds*1.5))
        host.send_json({'action':'start','version':state['version']})
        state = receive(host,'state',state['version']+1)
        assert state['turn_seconds'] is None and state['seconds_left'] is None
        host.send_json({'action':'timer','seconds':30,'version':state['version']})
        assert '本局结束' in receive(host,'error')['message']
        version = prepare_deal(client,room,playing=True)
        state = receive(host,'state',version)
        assert state['seconds_left'] is None and state['turn_seconds'] is None


@pytest.mark.parametrize('phase', ['burying', 'playing'])
@pytest.mark.parametrize('automatic', ['auto', 'empty_seat', 'disconnect'])
def test_no_timer_allows_ai_but_pauses_for_disconnected_humans(monkeypatch, phase, automatic):
    monkeypatch.setattr(server,'ROOM_TICK',.005)
    monkeypatch.setattr(server,'AI_DELAY',0)

    class Socket:
        async def send_json(self, state): pass

    async def scenario():
        game = Game(7); game.start(); finish_ai_deal(game)
        if phase == 'playing':
            action, ids = game.ai_action(game.turn); game.act(game.turn,action,ids)
        seat = game.turn
        room = server.Room('TIMERX',game=game,turn_seconds=None,
                           players={i:server.Player(str(i),socket=Socket()) for i in range(4)})
        room.changed = time.monotonic() - 1000000
        server.rooms[room.code] = room
        original = game.version
        task = asyncio.create_task(server.run_room(room))
        try:
            await asyncio.sleep(.03)
            assert game.version == original
            assert room.view(seat)['seconds_left'] is None
            if automatic == 'auto': room.players[seat].auto = True
            elif automatic == 'empty_seat': room.players.pop(seat)
            else: room.players[seat].socket = None
            await asyncio.sleep(.03)
            if automatic == 'disconnect':
                assert game.version == original and room.paused
            else:
                assert game.version > original
            assert not any('超时' in event for event in game.events)
        finally:
            task.cancel(); await asyncio.gather(task,return_exceptions=True)
            server.rooms.pop(room.code,None)
    asyncio.run(scenario())


@pytest.mark.parametrize('seconds', [15,30,60,120])
def test_configured_duration_and_deal_clock_are_independent(seconds):
    room = server.Room('CLOCKX',turn_seconds=seconds)
    room.game.start(); room.sync_deal_clock(0)
    assert room.next_draw_at == .5
    room.game.phase = 'playing'; assert room.timeout() == seconds
    room.game.phase = 'burying'; assert room.timeout() == math.ceil(seconds*1.5)


def test_no_timer_persists_across_next_round_and_restart(client):
    info = create(client); room = server.rooms[info['room']]
    with ExitStack() as stack:
        host, _, state = connect(client,stack,info['room'],info['token'])
        host.send_json({'action':'timer','seconds':None,'version':state['version']})
        state = receive(host,'state',state['version']+1)
        async def end(phase):
            async with room.lock:
                room.game.phase=phase
                room.game.result={'next_dealer':0}
        for phase, action in [('round_end','next'),('match_end','restart')]:
            client.portal.call(end,phase)
            host.send_json({'action':action,'version':state['version']})
            state=receive(host,'state',state['version']+1)
            assert state['turn_seconds'] is None and state['phase']=='dealing'
