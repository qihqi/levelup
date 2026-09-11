from contextlib import ExitStack
import json

from fastapi.testclient import TestClient
import pytest

from levelup import server


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "AI_DELAY", 100000)
    monkeypatch.setattr(server, "ACTION_INTERVAL", 0)
    monkeypatch.setattr(server, "DRAW_INTERVAL", 100000)
    monkeypatch.setattr(server, "AI_BID_DELAY", 100000)
    with TestClient(server.app) as c:
        yield c


def create(client, name="阿南"):
    response = client.post("/api/rooms", json={"name": name})
    assert response.status_code == 200
    return response.json()


def receive(ws, kind, version=0):
    for _ in range(30):
        message = ws.receive_json()
        if message["type"] == kind and message.get("version", version) >= version:
            return message
    pytest.fail(f"No {kind} message")


def connect(client, stack, room, token="", name="牌友"):
    ws = stack.enter_context(client.websocket_connect(f"/ws/{room}"))
    ws.send_json({"name": name, "token": token})
    welcome = receive(ws, "welcome")
    state = receive(ws, "state")
    return ws, welcome, state


def prepare_deal(client, room, finish=False, playing=False):
    """Advance the test clock's card events atomically; production uses Room's timer."""
    async def prepare():
        async with room.lock:
            while room.game.draw_pile:
                room.game.draw_card()
            if finish or playing:
                if not room.game.bid:
                    for seat in range(4):
                        action, ids = room.game.ai_action(seat, "basic")
                        if action == "bid":
                            room.game.act(seat, action, ids)
                            break
                room.game.finish_dealing()
            if playing:
                action, ids = room.game.ai_action(room.game.turn)
                room.game.act(room.game.turn, action, ids)
                room.game.turn = 0  # Put the human under test on lead.
            await room.broadcast()
            return room.game.version
    return client.portal.call(prepare)


def test_http_assets_and_origin_validation(client):
    assert client.get("/").status_code == 200
    assert "升级" in client.get("/").text
    for path in ("app.js", "style.css"):
        assert client.get(f"/static/{path}").status_code == 200
    assert client.get("/health").json()["status"] == "ok"
    assert client.post("/api/rooms", json={"name": " "}).status_code == 422
    assert client.post("/api/rooms", json={"name": "南"}, headers={"origin": "https://evil.example"}).status_code == 403


def test_private_legal_options_and_forced_move_submission(client):
    from levelup.game import Card

    info = create(client)
    room = server.rooms[info['room']]
    with ExitStack() as stack:
        host, _, initial = connect(client, stack, info['room'], info['token'])
        guest, _, _ = connect(client, stack, info['room'])
        assert initial['play_options'] is None

        async def prepare():
            async with room.lock:
                game = room.game
                game.phase, game.trump, game.turn = 'playing', 'S', 0
                game.hands = [[Card(0, 'H', 3), Card(54, 'H', 3), Card(1, 'C', 9)],
                              [Card(2, 'C', 4), Card(56, 'C', 4), Card(3, 'S', 8)],
                              [Card(4, 'D', 4), Card(58, 'D', 4), Card(7, 'C', 8)],
                              [Card(5, 'D', 5), Card(59, 'D', 5), Card(8, 'C', 9)]]
                game.trick = [(3, [Card(6, 'H', 7), Card(60, 'H', 7)])]
                game.version += 1
                await room.broadcast()
                return game.version

        version = client.portal.call(prepare)
        state = receive(host, 'state', version)
        options = state['play_options']
        assert options == {'count': 2, 'pool': [0, 54], 'required': [[0, 54]], 'forced': [0, 54]}
        assert receive(guest, 'state', version)['play_options'] is None
        host.send_json({'action': 'play', 'ids': options['forced'], 'version': version})
        state = receive(host, 'state', version + 1)
        assert state['turn'] == 1 and state['play_options'] is None
        assert [c['id'] for c in state['hand']] == [1]
        state = receive(guest, 'state', version + 1)
        assert state['play_options']['required'] == [[]]
        assert set(state['play_options']['pool']) == {2, 56, 3}
        assert state['play_options']['forced'] is None


def test_concurrent_bid_accepts_draw_stale_version_only_in_same_deal(client):
    from tests.test_dealing import ordered_game

    info = create(client)
    room = server.rooms[info['room']]
    with ExitStack() as stack:
        host, _, _ = connect(client, stack, info['room'], info['token'])
        guest, _, _ = connect(client, stack, info['room'])

        async def prepare():
            async with room.lock:
                room.game = ordered_game()
                room.game.version += 10
                room.sync_deal_clock(server.time.monotonic())
                for _ in range(6):
                    room.game.draw_card()
                await room.broadcast()
                return room.game.version, room.game.deal_id

        version, deal_id = client.portal.call(prepare)
        receive(host, 'state', version)
        receive(guest, 'state', version)
        host.send_json({'action': 'bid', 'ids': [0], 'version': version - 2, 'deal_id': deal_id})
        assert receive(host, 'state', version + 1)['bid']['seat'] == 0
        # Both clients submitted from the same older view; stronger bid still wins.
        guest.send_json({'action': 'bid', 'ids': [13, 67], 'version': version - 2, 'deal_id': deal_id})
        state = receive(guest, 'state', version + 2)
        assert state['bid']['seat'] == 1 and state['turn'] == 2 and state['dealt'] == 6
        host.send_json({'action': 'bid', 'ids': [0, 54], 'version': version, 'deal_id': deal_id})
        assert '更强' in receive(host, 'error')['message']
        for bad_id in (deal_id - 1, str(deal_id), True, None):
            host.send_json({'action': 'hint', 'version': version, 'deal_id': bad_id})
            assert '更新' in receive(host, 'error')['message']
        guest.send_json({'action': 'hint', 'version': version, 'deal_id': deal_id})
        assert receive(guest, 'hint')['deal_id'] == deal_id

        async def expire():
            async with room.lock:
                room.deal_closes_at = server.time.monotonic() - 1
        # Pause the actor itself, not client processing.
        client.portal.call(room.task.cancel)
        client.portal.call(expire)
        guest.send_json({'action': 'bid', 'ids': [13, 67], 'version': state['version'], 'deal_id': deal_id})
        assert '时间已结束' in receive(guest, 'error')['message']


def test_strategy_catalog_creation_validation_and_room_choice(client):
    catalog = client.get("/api/ai-strategies").json()
    assert catalog["default"] == "rule_based"
    assert {s["id"] for s in catalog["strategies"]} == {"basic", "rule_based"}
    assert client.post("/api/rooms", json={"name": "南", "ai_strategy": "missing"}).status_code == 422
    assert client.post("/api/rooms", json={"name": "南", "ai_strategy": []}).status_code == 422
    info = client.post("/api/rooms", json={"name": "南", "ai_strategy": "basic"}).json()
    with ExitStack() as stack:
        host, _, _ = connect(client, stack, info["room"], info["token"])
        guest, _, state = connect(client, stack, info["room"])
        assert state["ai_strategy"] == "basic"
        guest.send_json({"action": "ai_strategy", "strategy": "rule_based", "version": state["version"]})
        assert "房主" in receive(guest, "error")["message"]
        host.send_json({"action": "ai_strategy", "strategy": [], "version": state["version"]})
        assert "未知" in receive(host, "error")["message"]
        host.send_json({"action": "ai_strategy", "strategy": "rule_based", "version": state["version"]})
        state = receive(host, "state", state["version"] + 1)
        assert state["ai_strategy"] == "rule_based"
        assert receive(guest, "state", state["version"])["ai_strategy"] == "rule_based"
        host.send_json({"action": "start", "version": state["version"]})
        state = receive(host, "state", state["version"] + 1)
        host.send_json({"action": "ai_strategy", "strategy": "basic", "version": state["version"]})
        assert "切换" in receive(host, "error")["message"]
        host.send_json({"action": "hint", "version": state["version"]})
        hint = receive(host, "hint")
        assert hint["strategy"] == "rule_based" and hint["alternatives"]
        assert all(set(a["ids"]) <= {c["id"] for c in state["hand"]} for a in hint["alternatives"])
        assert hint["score"] == hint["alternatives"][0]["score"]


def test_four_human_clients_play_complete_round_privately(client):
    info = create(client)
    room = server.rooms[info["room"]]
    with ExitStack() as stack:
        connections = [connect(client, stack, info["room"], info["token"] if i == 0 else "", f"牌友{i}") for i in range(4)]
        sockets = [item[0] for item in connections]
        states = [receive(ws, "state", room.game.version) if i < 3 else connections[i][2] for i, ws in enumerate(sockets)]
        assert all(len([p for p in state["players"] if p["human"]]) == 4 for state in states)
        sockets[0].send_json({"action": "start", "version": room.game.version})
        states = [receive(ws, "state", room.game.version + (room.game.phase == "lobby")) for ws in sockets]
        assert all(s["phase"] == "dealing" and not s["hand"] for s in states)
        version = prepare_deal(client, room)
        states = [receive(ws, "state", version) for ws in sockets]
        hands = [{c["id"] for c in s["hand"]} for s in states]
        assert len(set.union(*hands)) == 100
        assert all(len(hand) == 25 for hand in hands)
        for s in states:
            encoded = json.dumps(s)
            assert "token" not in encoded and "hands" not in s and not s["bottom"]
        version = prepare_deal(client, room, finish=True)
        states = [receive(ws, "state", version) for ws in sockets]
        for _ in range(150):
            if states[0]["phase"] == "round_end":
                break
            seat = states[0]["turn"]
            version = states[0]["version"]
            ws = sockets[seat]
            ws.send_json({"action": "hint", "version": version})
            hint = receive(ws, "hint")
            assert set(hint["ids"]) <= {c["id"] for c in states[seat]["hand"]}
            ws.send_json({"action": hint["action"], "ids": hint["ids"], "version": version})
            states = [receive(sock, "state", version + 1) for sock in sockets]
            assert len({s["version"] for s in states}) == 1
        assert states[0]["phase"] == "round_end"
        assert all(not s["hand"] for s in states)
        assert all(len(s["bottom"]) == 8 for s in states)
        assert all(s["result"] == states[0]["result"] for s in states)
        version = states[0]["version"]
        sockets[0].send_json({"action": "next", "version": version})
        state = receive(sockets[0], "state", version + 1)
        assert state["round"] == 2 and state["phase"] == "dealing"


def test_stale_version_wrong_seat_malformed_and_forged_cards(client):
    info = create(client)
    room = server.rooms[info["room"]]
    with ExitStack() as stack:
        host, _, _ = connect(client, stack, info["room"], info["token"])
        guest, _, state = connect(client, stack, info["room"], name="阿东")
        guest.send_json({"action": "start", "version": state["version"]})
        assert "房主" in receive(guest, "error")["message"]
        host.send_json({"action": "start", "version": room.game.version})
        state = receive(host, "state", room.game.version + (room.game.phase == "lobby"))
        guest.send_json({"action": "pass", "version": state["version"], "bid_revision": state['bid_revision']})
        assert "摸牌完成" in receive(guest, "error")["message"]
        host.send_json({"action": "pass", "version": -1})
        assert "更新" in receive(host, "error")["message"]
        receive(host, "state")
        host.send_json({"action": "bid", "ids": [999], "version": room.game.version})
        assert "手牌" in receive(host, "error")["message"]
        host.send_text("not json")
        assert "JSON" in receive(host, "error")["message"]
        host.send_json(["play"])
        assert "格式" in receive(host, "error")["message"]
        host.send_json({"action": []})
        assert "操作类型" in receive(host, "error")["message"]
        host.send_json({"action": "ping"})
        receive(host, "pong")


def test_disconnect_reconnect_identity_and_host_transfer(client):
    info = create(client)
    room = server.rooms[info["room"]]
    with ExitStack() as stack:
        host, welcome, _ = connect(client, stack, info["room"], info["token"])
        guest, _, state = connect(client, stack, info["room"], name="阿东")
        version = state["version"]
        host.close()
        state = receive(guest, "state", version + 1)
        assert state["host"] == 1
        assert not state["players"][0]["connected"] and state["players"][0]["auto"]
        restored, welcome2, state = connect(client, stack, info["room"], welcome["token"])
        assert welcome2["seat"] == 0
        assert state["players"][0]["connected"] and not state["players"][0]["auto"]
        assert len(room.players) == 2


def test_lobby_seat_change_and_capacity(client):
    info = create(client)
    with ExitStack() as stack:
        host, _, state = connect(client, stack, info["room"], info["token"])
        host.send_json({"action": "seat", "seat": 2, "version": state["version"]})
        state = receive(host, "state", state["version"] + 1)
        assert state["seat"] == state["host"] == 2
        for i in range(3):
            connect(client, stack, info["room"], name=f"Guest{i}")
        with client.websocket_connect(f"/ws/{info['room']}") as fifth:
            fifth.send_json({"name": "第五人"})
            assert "四位" in receive(fifth, "error")["message"]


def test_unknown_room_bad_token_and_join_after_start(client):
    with client.websocket_connect("/ws/ZZZZZZ") as ws:
        assert "不存在" in receive(ws, "error")["message"]
    info = create(client)
    with client.websocket_connect(f"/ws/{info['room']}") as ws:
        ws.send_json({"name": "冒名", "token": "wrong"})
        assert "身份" in receive(ws, "error")["message"]
    with ExitStack() as stack:
        host, _, state = connect(client, stack, info["room"], info["token"])
        host.send_json({"action": "start", "version": state["version"]})
        receive(host, "state", state["version"] + 1)
        with client.websocket_connect(f"/ws/{info['room']}") as newcomer:
            newcomer.send_json({"name": "迟到"})
            assert "已经开始" in receive(newcomer, "error")["message"]


def test_live_ai_takes_over_disconnected_turn(client, monkeypatch):
    info = create(client)
    with ExitStack() as stack:
        host, _, _ = connect(client, stack, info["room"], info["token"])
        guest, _, state = connect(client, stack, info["room"], name="阿东")
        host.send_json({"action": "start", "version": state["version"]})
        state = receive(host, "state", state["version"] + 1)
        version = prepare_deal(client, server.rooms[info["room"]], playing=True)
        state = receive(host, "state", version)
        monkeypatch.setattr(server, "AI_DELAY", 0)
        host.close()
        state = receive(guest, "state", version + 2)
        assert state["turn"] == 1 and state["players"][0]["auto"]


def test_turn_timeout_takes_one_action_without_permanent_autoplay(client, monkeypatch):
    info = create(client)
    with ExitStack() as stack:
        host, _, state = connect(client, stack, info["room"], info["token"])
        host.send_json({"action": "start", "version": state["version"]})
        state = receive(host, "state", state["version"] + 1)
        version = prepare_deal(client, server.rooms[info["room"]], playing=True)
        server.rooms[info["room"]].turn_seconds = 0
        state = receive(host, "state", version + 1)
        assert state["turn"] == 1 and not state["players"][0]["auto"]
        assert any("超时" in event for event in state["events"])


def test_final_bid_concurrent_passes_reset_on_counterbid(client):
    from tests.test_dealing import ordered_game

    info = create(client)
    room = server.rooms[info["room"]]
    with ExitStack() as stack:
        host, _, _ = connect(client, stack, info["room"], info["token"])
        guest, _, _ = connect(client, stack, info["room"])

        async def prepare():
            async with room.lock:
                room.game = ordered_game()
                while room.game.draw_pile:
                    room.game.draw_card()
                room.sync_deal_clock(server.time.monotonic())
                await room.broadcast()
                return room.game.version

        version = client.portal.call(prepare)
        state = receive(host, "state", version)
        receive(guest, "state", version)
        command = {"action": "pass", "version": version, "deal_id": state["deal_id"],
                   "bid_revision": state["bid_revision"]}
        host.send_json(command)
        receive(host, "state", version + 1)
        guest.send_json(command)  # Same original version, but same declaration: accept.
        state = receive(guest, "state", version + 2)
        assert state["bid_passed"] == [0, 1]
        guest.send_json({"action": "bid", "ids": [13, 67], "version": state["version"],
                         "deal_id": state["deal_id"]})
        state = receive(guest, "state", version + 3)
        assert state["bid_passed"] == [] and state["bid_revision"] == 1
        assert state["deal_seconds_left"] == 60
        host.send_json(command)  # Previous confirmation cannot carry across a new bid.
        assert "更新" in receive(host, "error")["message"]
        assert room.game.bid_passed == set()


def test_heartbeat_does_not_consume_or_reset_action_rate_limit(client, monkeypatch):
    # A long interval makes this deterministic without sleeps or a mocked clock.
    monkeypatch.setattr(server, "ACTION_INTERVAL", 1000)
    info = create(client)
    with ExitStack() as stack:
        host, _, state = connect(client, stack, info["room"], info["token"])
        host.send_json({"action": "ping"})
        assert host.receive_json()["type"] == "pong"
        host.send_json({"action": "timer", "seconds": None, "version": state["version"]})
        state = host.receive_json()
        assert state["type"] == "state" and state["turn_seconds"] is None
        # A ping immediately after a click must also return pong, not an error.
        host.send_json({"action": "ping"})
        assert host.receive_json()["type"] == "pong"
        host.send_json({"action": "timer", "seconds": 30, "version": state["version"]})
        assert "操作太快" in host.receive_json()["message"]
        assert server.rooms[info["room"]].turn_seconds is None
