from contextlib import ExitStack
import asyncio
import json
import stat
import threading

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from levelup import server, security, ws_logging
from tests.test_server import client, create, connect, receive, prepare_deal
from tests.test_ws_logging import audit


@pytest.mark.parametrize('origin', ['https://evil.example', 'null', '', 'http://[',
                                  'ftp://testserver', 'http://testserver/path',
                                  'http://testserver#other'])
def test_bad_origins_are_rejected_without_server_errors(client, origin):
    assert client.post('/api/rooms', json={'name': '南'}, headers={'origin': origin}).status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/ws/ABCDEF', headers={'origin': origin}):
            pass


def test_http_limits_and_headers(client, monkeypatch):
    assert client.post('/api/rooms', content=b' ' * (security.MAX_HTTP_BODY + 1)).status_code == 413
    for path in ('/', '/static/app.js', '/static/style.css', '/health', '/.env', '/logs/example.jsonl'):
        response = client.get(path)
        assert response.headers['x-frame-options'] == 'DENY'
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert "script-src 'self'" in response.headers['content-security-policy']
        if path.startswith(('/.env', '/logs')):
            assert response.status_code == 404
    monkeypatch.setattr(security, 'ROOM_CREATIONS_PER_MINUTE', 1)
    with TestClient(server.app, client=('rate-limit-test', 1)) as limited:
        assert limited.post('/api/rooms', json={'name': '南'}).status_code == 200
        response = limited.post('/api/rooms', json={'name': '南'})
        assert response.status_code == 429 and response.headers['retry-after'] == '60'


@pytest.mark.parametrize('token', ['中文', 'x' * 129, ['not-a-string']])
def test_malformed_identity_does_not_claim_a_seat(client, token):
    info = create(client)
    with client.websocket_connect('/ws/' + info['room']) as ws:
        ws.send_json({'name': '南', 'token': token})
        assert '格式错误' in ws.receive_json()['message']
    assert len(server.rooms[info['room']].players) == 1


def test_web_strategy_allowlist_cannot_be_bypassed_by_direct_requests(client, monkeypatch):
    monkeypatch.setenv('LEVELUP_WEB_AI_STRATEGIES', 'rule_based')
    assert [s['id'] for s in client.get('/api/ai-strategies').json()['strategies']] == ['rule_based']
    assert client.post('/api/rooms', json={'name': '南', 'ai_strategy': 'openai_agent'}).status_code == 422
    info = create(client)
    with ExitStack() as stack:
        ws, _, state = connect(client, stack, info['room'], info['token'])
        ws.send_json({'action': 'ai_strategy', 'strategy': 'openai_agent', 'version': state['version']})
        assert '未开放' in receive(ws, 'error')['message']
        assert server.rooms[info['room']].ai_strategy == 'rule_based'


def test_websocket_invalid_frames_depth_size_and_flood(client, monkeypatch):
    info = create(client)
    with ExitStack() as stack:
        ws, _, _ = connect(client, stack, info['room'], info['token'])
        ws.send_text('[' * 1100 + '0' + ']' * 1100)
        assert receive(ws, 'error')['type'] == 'error'
        ws.send_json({'action': 'ping'})
        assert receive(ws, 'pong')['type'] == 'pong'
        ws.send_text(' ' * (ws_logging.MAX_MESSAGE_BYTES + 1))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1009
    with ExitStack() as stack:
        ws, _, _ = connect(client, stack, info['room'], info['token'])
        ws.send_bytes(b'binary')
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1003
    monkeypatch.setattr(ws_logging, 'MESSAGE_BURST', 2)
    monkeypatch.setattr(ws_logging, 'MESSAGES_PER_SECOND', 0)
    with ExitStack() as stack:
        ws, _, _ = connect(client, stack, info['room'], info['token'])
        ws.send_json({'action': 'ping'})
        receive(ws, 'pong')
        ws.send_json({'action': 'ping'})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1008


def test_log_redaction_and_private_permissions(audit):
    value = 'sk-proj-' + 'a' * 40
    fields = {'api_key': value, 'OPENAI_API_KEY': value, 'access-token': value,
              'nested': [{'clientSecret': value}], 'model_output': 'accidental ' + value}
    socket = ws_logging.LoggedWebSocket(None, 'ABCDEF', lambda: None)
    socket.record('test', **fields)
    assert value not in json.dumps(audit())
    from pathlib import Path
    path = Path(ws_logging.logger.handlers[0].baseFilename)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    ws_logging.logger.handlers[0].maxBytes = 1
    socket.record('rotated')
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in path.parent.glob(path.name + '*'))


def test_slow_hint_is_only_started_once_per_turn(client, monkeypatch):
    entered = threading.Event()
    calls = []
    async def slow(*args, **kwargs):
        calls.append(1)
        entered.set()
        await asyncio.sleep(10)
    monkeypatch.setattr(server, 'search_ranking', slow)
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
        ws.send_json({'action': 'hint', 'version': version})
        assert '已请求过' in receive(ws, 'error')['message']
        assert calls == [1]
        ws.send_json({'action': 'ping'})
        receive(ws, 'pong')
