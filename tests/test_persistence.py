"""Real SQLite/restart tests, including membership authorization and frozen clocks."""
import asyncio
from contextlib import ExitStack
import json
import os
import sqlite3
import time

from fastapi.testclient import TestClient
import pytest

from levelup import server
from levelup.ai.simulation import finish_ai_deal
from levelup.game import Game
from levelup.storage import Store, game_snapshot, restore_game
from tests.test_server import client, receive, prepare_deal


def join_identity(client, stack, code, identity, name=None):
    ws = stack.enter_context(client.websocket_connect(f"/ws/{code}"))
    hello = {"identity": identity}
    if name is not None:
        hello["name"] = name
    ws.send_json(hello)
    return ws, receive(ws, "welcome"), receive(ws, "state")


def pending(client, identity):
    response = client.get("/api/me/rooms", headers={"Authorization": f"Bearer {identity}"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def saved(code):
    with sqlite3.connect(os.environ["LEVELUP_DB_PATH"]) as db:
        return json.loads(db.execute("SELECT snapshot FROM rooms WHERE code=?", (code,)).fetchone()[0])


@pytest.mark.parametrize("phase", ["lobby", "drawing", "declaring", "burying", "playing", "round_end"])
def test_game_snapshot_roundtrip_preserves_rng_and_every_card(phase):
    game = Game(43)
    if phase != "lobby":
        game.start()
        for _ in range(13 if phase == "drawing" else 100):
            game.draw_card()
        if phase not in ("drawing", "declaring"):
            finish_ai_deal(game)
        if phase in ("playing", "round_end"):
            action, ids = game.ai_action(game.turn, "basic")
            game.act(game.turn, action, ids)
            if phase == "playing":
                action, ids = game.ai_action(game.turn, "basic")
                game.act(game.turn, action, ids)
            while phase == "round_end" and game.phase == "playing":
                action, ids = game.ai_action(game.turn, "basic")
                game.act(game.turn, action, ids)
    payload = json.dumps(game_snapshot(game), sort_keys=True)
    restored = restore_game(json.loads(payload))
    assert json.dumps(game_snapshot(restored), sort_keys=True) == payload
    assert restored.rng.random() == game.rng.random()
    for seat in range(4):
        assert restored.view(seat) == game.view(seat)


def test_identity_list_is_private_name_is_saved_and_database_is_private(client):
    identity = client.post("/api/identity", json={"name": "小南"}).json()["identity"]
    first = client.post("/api/rooms", json={"identity": identity, "name": "新昵称"}).json()
    second = client.post("/api/rooms", json={"identity": identity, "name": "新昵称"}).json()
    other = client.post("/api/rooms", json={"name": "同名也不是本人"}).json()
    result = pending(client, identity)
    assert result["name"] == "新昵称"
    assert {r["room"] for r in result["rooms"]} == {first["room"], second["room"]}
    assert [r["room"] for r in pending(client, other["identity"])["rooms"]] == [other["room"]]
    assert client.get("/api/me/rooms").status_code == 401
    assert client.get("/api/me/rooms", headers={"Authorization": "Bearer " + "x" * 43}).status_code == 401
    serialized = json.dumps(result)
    assert all(key not in serialized for key in (identity, '"token"', '"hand"', '"bottom"', '"identity"'))
    from pathlib import Path
    assert Path(os.environ["LEVELUP_DB_PATH"]).stat().st_mode & 0o777 == 0o600


def test_server_restart_restores_cards_and_waits_for_both_identities(monkeypatch):
    monkeypatch.setattr(server, "AI_DELAY", 100000)
    monkeypatch.setattr(server, "DRAW_INTERVAL", 100000)
    monkeypatch.setattr(server, "AI_BID_DELAY", 100000)
    monkeypatch.setattr(server, "ACTION_INTERVAL", 0)
    with TestClient(server.app) as c, ExitStack() as stack:
        info = c.post("/api/rooms", json={"name": "庄家"}).json()
        guest_id = c.post("/api/identity", json={"name": "队友"}).json()["identity"]
        host, _, _ = join_identity(c, stack, info["room"], info["identity"])
        guest, _, state = join_identity(c, stack, info["room"], guest_id)
        host.send_json({"action": "start", "version": state["version"]})
        receive(host, "state", state["version"] + 1)
        room = server.rooms[info["room"]]

        async def partial_draw():
            async with room.lock:
                for _ in range(13):
                    room.game.draw_card()
                room.next_draw_at = time.monotonic() + 30
                await room.broadcast()
        c.portal.call(partial_draw)
        # Read another SQLite connection before clean shutdown: acknowledgments
        # have already committed the cards, not merely queued a shutdown save.
        before = saved(info["room"])
        assert before["game"]["dealt"] == 13
        assert [len(h) for h in before["game"]["hands"]] == [4, 3, 3, 3]
    assert not server.rooms
    with TestClient(server.app) as c, ExitStack() as stack:
        assert pending(c, info["identity"])["rooms"][0]["room"] == info["room"]
        assert pending(c, guest_id)["rooms"][0]["room"] == info["room"]
        host, welcome, state = join_identity(c, stack, info["room"], info["identity"])
        assert welcome["name"] == "庄家" and welcome["seat"] == 0
        assert state["paused"] and state["waiting_for"] == [1] and state["dealt"] == 13
        room = server.rooms[info["room"]]
        assert json.loads(json.dumps(game_snapshot(room.game)))["hands"] == before["game"]["hands"]
        assert json.loads(json.dumps(room.game.rng.getstate())) == before["game"]["rng"]
        assert room.next_draw_at - room.paused_at == pytest.approx(30, abs=.5)
        host.send_json({"action": "hint", "deal_id": state["deal_id"], "version": state["version"]})
        assert "暂停" in receive(host, "error")["message"]
        c.portal.call(asyncio.sleep, .12)
        assert room.game.dealt == 13
        _, _, state = join_identity(c, stack, info["room"], guest_id)
        assert not state["paused"] and state["dealt"] == 13
        assert room.next_draw_at - time.monotonic() == pytest.approx(30, abs=.5)


def test_saved_play_can_continue_with_same_bottom_and_history(client):
    info = client.post("/api/rooms", json={"name": "南"}).json()
    with ExitStack() as stack:
        host, _, state = join_identity(client, stack, info["room"], info["identity"])
        host.send_json({"action": "start", "version": state["version"]})
        receive(host, "state", state["version"] + 1)
        room = server.rooms[info["room"]]
        version = prepare_deal(client, room, playing=True)
        receive(host, "state", version)
        action, ids = room.game.ai_action(0, "basic")
        host.send_json({"action": action, "ids": ids, "version": version})
        state = receive(host, "state", version + 1)
        before = saved(info["room"])
        assert before["game"]["version"] == state["version"]
        assert before["game"]["trick"]
    # Eviction forces reconstruction from SQLite; no use of the old Game object.
    async def evict():
        room.task.cancel()
        await asyncio.gather(room.task, return_exceptions=True)
        server.rooms.pop(room.code)
    client.portal.call(evict)
    with ExitStack() as stack:
        _, _, restored = join_identity(client, stack, info["room"], info["identity"])
        assert restored["hand"] == state["hand"]
        assert restored["trick"] == state["trick"]
        rebuilt = server.rooms[info["room"]]
        assert rebuilt is not room and rebuilt.game is not room.game
        assert json.loads(json.dumps(game_snapshot(rebuilt.game)))["bottom"] == before["game"]["bottom"]


def test_only_starter_can_kick_and_removed_membership_cannot_resume(client):
    info = client.post("/api/rooms", json={"name": "房主"}).json()
    guest_id = client.post("/api/identity", json={"name": "来宾"}).json()["identity"]
    with ExitStack() as stack:
        host, _, _ = join_identity(client, stack, info["room"], info["identity"])
        guest, welcome, state = join_identity(client, stack, info["room"], guest_id)
        guest.send_json({"action": "kick", "seat": 0, "version": state["version"]})
        assert "原房主" in receive(guest, "error")["message"]
        host.send_json({"action": "start", "version": state["version"]})
        state = receive(host, "state", state["version"] + 1)
        guest.close()
        state = receive(host, "state", state["version"] + 1)
        assert state["paused"] and state["waiting_for"] == [1]
        host.send_json({"action": "kick", "seat": 1, "version": state["version"]})
        state = receive(host, "state", state["version"] + 1)
        assert not state["paused"] and not state["players"][1]["human"]
        assert state["players"][1]["auto"] and pending(client, guest_id)["rooms"] == []
        with client.websocket_connect(f"/ws/{info['room']}") as removed:
            removed.send_json({"identity": guest_id, "token": welcome["token"]})
            assert "身份已失效" in receive(removed, "error")["message"]
        with client.websocket_connect(f"/ws/{info['room']}") as removed:
            removed.send_json({"identity": guest_id})
            assert "已经开始" in receive(removed, "error")["message"]


def test_identity_cannot_use_another_players_room_token(client):
    info = client.post("/api/rooms", json={"name": "房主"}).json()
    other = client.post("/api/identity", json={"name": "房主"}).json()["identity"]
    with client.websocket_connect(f"/ws/{info['room']}") as ws:
        ws.send_json({"identity": other, "token": info["token"]})
        assert "不属于" in receive(ws, "error")["message"]
    assert len(server.rooms[info["room"]].players) == 1


def test_restored_strategy_respects_current_server_allowlist(client, monkeypatch):
    monkeypatch.delenv("LEVELUP_WEB_AI_STRATEGIES", raising=False)
    info = client.post("/api/rooms", json={"name": "房主", "ai_strategy": "openai_agent"}).json()
    room = server.rooms[info["room"]]
    async def evict():
        room.task.cancel()
        await asyncio.gather(room.task, return_exceptions=True)
        server.rooms.pop(room.code)
    client.portal.call(evict)
    monkeypatch.setenv("LEVELUP_WEB_AI_STRATEGIES", "rule_based")
    with ExitStack() as stack:
        _, _, state = join_identity(client, stack, info["room"], info["identity"])
        assert state["ai_strategy"] == "rule_based"
        assert saved(info["room"])["ai_strategy"] == "rule_based"


def test_kicking_disconnected_player_resumes_ai_on_their_turn(client, monkeypatch):
    info = client.post("/api/rooms", json={"name": "房主"}).json()
    guest_id = client.post("/api/identity", json={"name": "来宾"}).json()["identity"]
    with ExitStack() as stack:
        host, _, _ = join_identity(client, stack, info["room"], info["identity"])
        guest, _, state = join_identity(client, stack, info["room"], guest_id)
        host.send_json({"action": "start", "version": state["version"]})
        receive(host, "state", state["version"] + 1)
        room = server.rooms[info["room"]]
        prepare_deal(client, room, playing=True)
        async def guest_turn():
            async with room.lock:
                room.game.turn = 1
                room.game.version += 1
                await room.broadcast()
                return room.game.version
        state = receive(host, "state", client.portal.call(guest_turn))
        original_hand = len(room.game.hands[1])
        guest.close()
        state = receive(host, "state", state["version"] + 1)
        assert state["paused"]
        monkeypatch.setattr(server, "AI_DELAY", 0)
        host.send_json({"action": "kick", "seat": 1, "version": state["version"]})
        kicked = receive(host, "state", state["version"] + 1)
        assert not kicked["paused"]
        played = receive(host, "state", kicked["version"] + 1)
        assert played["trick"][0]["seat"] == 1
        assert len(room.game.hands[1]) < original_hand


def test_pause_freezes_turn_and_declaration_clocks(monkeypatch):
    monkeypatch.setattr(server.time, "monotonic", lambda: 100)
    room = server.Room("TIMER1", players={0: server.Player("南", socket=object()), 1: server.Player("东")})
    room.changed, room.next_draw_at, room.deal_closes_at = 90, 101, 150
    assert room.sync_presence()
    monkeypatch.setattr(server.time, "monotonic", lambda: 1000)
    assert room.view(0)["seconds_left"] == 50
    room.players[1].socket = object()
    assert room.sync_presence()
    assert room.changed == 990 and room.next_draw_at == 1001 and room.deal_closes_at == 1050


def test_finished_match_is_not_pending_and_identity_is_redacted(client):
    from levelup.ws_logging import redact
    info = client.post("/api/rooms", json={"name": "完成"}).json()
    async def finish():
        room = server.rooms[info["room"]]
        async with room.lock:
            room.game.phase = "match_end"
            await room.broadcast()
    client.portal.call(finish)
    assert pending(client, info["identity"])["rooms"] == []
    assert info["identity"] not in json.dumps(redact({"identity": info["identity"]}))
