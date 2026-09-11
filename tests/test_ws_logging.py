from contextlib import ExitStack
import json
import logging

import pytest

from levelup import server
from levelup import ws_logging
from tests.test_server import client, create, connect, receive
from levelup.game import Card


@pytest.fixture
def audit(tmp_path, monkeypatch):
    logger = logging.Logger("levelup.websocket.test")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    monkeypatch.setattr(ws_logging, "logger", logger)
    monkeypatch.setenv("LEVELUP_LOG_DIR", str(tmp_path))
    yield lambda: [json.loads(line) for path in tmp_path.glob("*.jsonl")
                   for line in path.read_text().splitlines()]
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)


def test_requests_broadcasts_errors_and_unsolicited_updates(client, audit):
    info = create(client)
    room = server.rooms[info["room"]]
    with ExitStack() as stack:
        host, welcome, _ = connect(client, stack, info["room"], info["token"])
        assert welcome["token"] == info["token"]  # Redact only the log, not the wire.
        guest, _, state = connect(client, stack, info["room"])
        host.send_json({"action": "timer", "seconds": None, "version": state["version"]})
        receive(host, "state", state["version"] + 1)
        receive(guest, "state", state["version"] + 1)
        host.send_json({"action": "play", "ids": [99], "version": -1})
        receive(host, "error")
        receive(host, "state")
        host.send_text('{"token":"malformed-secret"')
        receive(host, "error")
        host.send_json({"action": "ping", "nested": {"token": "nested-secret"}})
        receive(host, "pong")
        client.portal.call(room.broadcast)
        receive(host, "state")
        receive(guest, "state")

        rows = audit()
        assert all(row["room"] == info["room"] for row in rows)
        encoded = json.dumps(rows)
        for secret in (info["token"], welcome["token"], "malformed-secret", "nested-secret"):
            assert secret not in encoded
        requests = [r for r in rows if r["event"] == "request"]
        assert len({r["request_id"] for r in requests}) == len(requests)
        assert any(r["event"] == "response" and r["payload"]["type"] == "welcome"
                   and r["request_id"] == requests[0]["request_id"] for r in rows)
        timer = next(r for r in requests if r.get("payload", {}).get("action") == "timer")
        responses = [r for r in rows if r["event"] == "response" and r["request_id"] == timer["request_id"]]
        assert {r["payload"]["seat"] for r in responses} == {0, 1}
        assert len({r["connection_id"] for r in responses}) == 2
        rejected = next(r for r in requests if r.get("payload", {}).get("action") == "play")
        assert rejected["context"]["version"] > rejected["payload"]["version"]
        assert {r["payload"]["type"] for r in rows if r["event"] == "response"
                and r["request_id"] == rejected["request_id"]} == {"error", "state"}
        malformed = next(r for r in requests if r.get("invalid_json"))
        assert any(r["event"] == "response" and r["request_id"] == malformed["request_id"]
                   and r["payload"]["type"] == "error" for r in rows)
        assert all(r["request_id"] is None for r in rows[-2:])


def test_rejected_follow_retains_hand_lead_trump_and_reason(client, audit):
    info = create(client)
    room = server.rooms[info["room"]]
    with ExitStack() as stack:
        host, _, _ = connect(client, stack, info["room"], info["token"])

        async def prepare():
            async with room.lock:
                game = room.game
                game.phase = "playing"
                game.trump = "H"
                game.hands[0] = [Card(1, "S", 3), Card(27, "C", 3)]
                game.trick = [(3, [Card(2, "S", 4)])]
                game.turn = 0
                game.version += 1
                await room.broadcast()
                return game.version

        version = client.portal.call(prepare)
        receive(host, "state", version)
        host.send_json({"action": "play", "ids": [27], "version": version})
        error = receive(host, "error")
        assert "先跟足领出花色" in error["message"]
        rows = audit()
        request = next(r for r in rows if r["event"] == "request" and r["payload"].get("action") == "play")
        state = request["context"]
        assert {c["id"] for c in state["hand"]} == {1, 27}
        assert state["trick"][0]["cards"][0]["id"] == 2
        assert state["level"] == "2" and state["trump"] == "H"
        assert any(r["event"] == "response" and r["request_id"] == request["request_id"]
                   and r["payload"] == error for r in rows)


def test_rotation_keeps_valid_json_lines(audit):
    ws_logging.configure()
    ws_logging.logger.handlers[0].maxBytes = 300
    socket = ws_logging.LoggedWebSocket(None, "ABCDEF", lambda: None)
    for i in range(20):
        socket.record("test", sequence=i)
    handler = ws_logging.logger.handlers[0]
    from pathlib import Path
    path = Path(handler.baseFilename)
    assert path.with_name(path.name + ".1").exists()
    assert json.loads(path.read_text().splitlines()[-1])["sequence"] == 19
