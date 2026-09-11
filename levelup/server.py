"""In-memory rooms, private player views, and serialized WebSocket actions.

Run a single worker: a room and its sockets must live in the same process.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import logging
import math
import secrets
import time

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .assets import static_dir
from .legal import play_options
from .game import Game, RuleError
from .ai import DEFAULT_STRATEGY, get_strategy, strategy_catalog
from .ws_logging import LoggedWebSocket, request_id as audit_request_id

STATIC_DIR = static_dir()
log = logging.getLogger("levelup")
AI_DELAY = 1.1
TURN_SECONDS = 60
TIMER_OPTIONS = (15, 30, 60, 120, None)
ROOM_TTL = 6 * 3600
MAX_ROOMS = 200
ACTION_INTERVAL = 0.08
DRAW_INTERVAL = 0.5
DEAL_CLOSE_SECONDS = 60.0
AI_BID_DELAY = 0.7
ROOM_TICK = 0.05


@dataclass
class Player:
    name: str
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    socket: WebSocket | None = None
    auto: bool = False


@dataclass
class Room:
    code: str
    game: Game = field(default_factory=Game)
    players: dict[int, Player] = field(default_factory=dict)
    host: int = 0
    ai_strategy: str = DEFAULT_STRATEGY
    turn_seconds: int | None = TURN_SECONDS
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    touched: float = field(default_factory=time.monotonic)
    changed: float = field(default_factory=time.monotonic)
    task: asyncio.Task | None = None
    dealing_id: int | None = None
    next_draw_at: float = 0.0
    deal_closes_at: float | None = None
    closing_bid_count: int = 0
    ai_bid_checks: dict = field(default_factory=dict)

    def mark(self):
        self.changed = self.touched = time.monotonic()

    def automated(self, seat):
        p = self.players.get(seat)
        return p is None or p.socket is None or p.auto

    def sync_deal_clock(self, now, resume=False):
        if self.game.phase == "dealing" and (resume or self.dealing_id != self.game.deal_id):
            self.dealing_id = self.game.deal_id
            self.next_draw_at = now + DRAW_INTERVAL
            self.deal_closes_at = None if self.game.draw_pile else now + DEAL_CLOSE_SECONDS
            self.ai_bid_checks.clear()
            self.closing_bid_count = len(self.game.declarations)
        if (self.game.phase == "dealing" and self.deal_closes_at is not None
                and self.closing_bid_count != len(self.game.declarations)):
            self.closing_bid_count = len(self.game.declarations)
            self.deal_closes_at = now + DEAL_CLOSE_SECONDS

    def advance_dealing(self, now):
        """Keep dealing independent of human actions, AI pauses and turn timers."""
        game = self.game
        if game.phase != "dealing":
            return False
        self.sync_deal_clock(now)
        changed = False
        if self.deal_closes_at is not None and (now >= self.deal_closes_at or len(game.bid_passed) == 4):
            game.finish_dealing()
            self.changed = self.touched = now
            self.sync_deal_clock(now)
            return True
        if game.draw_pile and now >= self.next_draw_at:
            game.draw_card()
            # Never dump a burst of missed cards on a reconnect or a slow client.
            self.next_draw_at = now + DRAW_INTERVAL
            if not game.draw_pile:
                self.deal_closes_at = now + DEAL_CLOSE_SECONDS
            changed = True
        for seat in range(4):
            if not game.draw_pile and seat in game.bid_passed:
                continue
            if not self.automated(seat):
                self.ai_bid_checks.pop(seat, None)
                continue
            signature = (game.deal_id, len(game.hands[seat]),
                         not game.draw_pile,
                         game.bid["seat"] if game.bid else None,
                         game.bid["value"] if game.bid else 0)
            old = self.ai_bid_checks.get(seat)
            if old is None or old[0] != signature:
                self.ai_bid_checks[seat] = (signature, now + AI_BID_DELAY)
                continue
            if now < old[1]:
                continue
            self.ai_bid_checks[seat] = (signature, math.inf)
            action, ids = game.ai_action(seat, self.ai_strategy)
            if action == "bid":
                game.act(seat, action, ids)
                changed = True
                break  # Re-evaluate other AIs against this declaration next tick.
            if not game.draw_pile:
                game.act(seat, "pass")
                changed = True
        self.sync_deal_clock(now)
        if not game.draw_pile and len(game.bid_passed) == 4:
            game.finish_dealing()
            self.changed = now
            self.sync_deal_clock(now)
            changed = True
        if changed:
            self.touched = now
        return changed

    def view(self, seat):
        state = self.game.view(seat)
        game = self.game
        state["play_options"] = play_options(
            game.rules, game.hands[seat], game.trick[0][1] if game.trick else ()
        ) if game.phase == "playing" and game.turn == seat else None
        state.update({"type": "state", "room": self.code, "seat": seat, "host": self.host,
                      "turn_seconds": self.turn_seconds,
                      "bury_seconds": None if self.turn_seconds is None else math.ceil(self.turn_seconds * 1.5),
                      "timer_options": list(TIMER_OPTIONS),
                      "ai_strategy": self.ai_strategy, "ai_strategies": strategy_catalog(),
                      "draw_interval": DRAW_INTERVAL,
                      "deal_close_seconds": DEAL_CLOSE_SECONDS,
                      "deal_closing": self.game.phase == "dealing" and self.deal_closes_at is not None,
                      "deal_seconds_left": max(0, math.ceil(self.deal_closes_at - time.monotonic()))
                          if self.game.phase == "dealing" and self.deal_closes_at is not None else None,
                      "players": [{"seat": i, "name": p.name if p else f"AI · {'南东北西'[i]}",
                                   "human": p is not None, "connected": bool(p and p.socket),
                                   "auto": self.automated(i)} for i in range(4)
                                  for p in [self.players.get(i)]],
                      "seconds_left": None if self.timeout() is None else
                          max(0, round(self.timeout() - (time.monotonic() - self.changed)))})
        return state

    def timeout(self):
        if self.turn_seconds is None:
            return None
        return math.ceil(self.turn_seconds * 1.5) if self.game.phase == "burying" else self.turn_seconds

    async def broadcast(self):
        async def send(seat, player):
            socket = player.socket
            if socket:
                try:
                    await asyncio.wait_for(socket.send_json(self.view(seat)), timeout=3)
                except (Exception, asyncio.TimeoutError):
                    if player.socket is socket:
                        player.socket = None
        await asyncio.gather(*(send(i, p) for i, p in self.players.items()))


rooms: dict[str, Room] = {}


async def run_room(room):
    try:
        while rooms.get(room.code) is room:
            await asyncio.sleep(ROOM_TICK)
            async with room.lock:
                connected = any(p.socket for p in room.players.values())
                if not connected:
                    if time.monotonic() - room.touched > ROOM_TTL:
                        rooms.pop(room.code, None)
                        return
                    continue  # Pause abandoned rooms instead of burning CPU playing AI rounds.
                game = room.game
                if game.phase == "dealing":
                    if room.advance_dealing(time.monotonic()):
                        await room.broadcast()
                    continue
                if game.phase not in ("burying", "playing"):
                    continue
                elapsed = time.monotonic() - room.changed
                automatic = room.automated(game.turn)
                limit = AI_DELAY if automatic else room.timeout()
                if limit is None or elapsed < limit:
                    continue
                action, ids = game.ai_action(game.turn, room.ai_strategy)
                if not automatic:
                    game.log(f"{game.turn + 1} 号位操作超时，AI 代行本次操作。")
                game.act(game.turn, action, ids)
                room.mark()
                await room.broadcast()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Room actor failed: %s", room.code)
        # Surface actor failure rather than leaving connected clients silently stuck.
        for p in room.players.values():
            if p.socket:
                try:
                    await p.socket.send_json({"type": "error", "message": "房间运行异常，请创建新房间。"})
                except Exception:
                    pass


@asynccontextmanager
async def lifespan(app):
    yield
    tasks = [r.task for r in rooms.values() if r.task]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    rooms.clear()


app = FastAPI(title="升级 · Levelup", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class CreateRoom(BaseModel):
    name: str = Field(min_length=1, max_length=20)
    ai_strategy: str = DEFAULT_STRATEGY


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok", "rooms": len(rooms)}


@app.get("/api/ai-strategies")
async def ai_strategies():
    return {"default": DEFAULT_STRATEGY, "strategies": strategy_catalog()}


def same_origin(origin, host):
    from urllib.parse import urlsplit
    return not origin or urlsplit(origin).netloc == host


@app.post("/api/rooms")
async def create_room(body: CreateRoom, request: Request):
    if not same_origin(request.headers.get("origin"), request.headers.get("host")):
        raise HTTPException(403, "请从游戏页面创建房间。")
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "请输入昵称。")
    try:
        get_strategy(body.ai_strategy)
    except RuleError as exc:
        raise HTTPException(422, str(exc)) from exc
    if len(rooms) >= MAX_ROOMS:
        raise HTTPException(503, "房间已满，请稍后再试。")
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    while code in rooms:
        code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    room = Room(code, ai_strategy=body.ai_strategy)
    room.players[0] = Player(name)
    rooms[code] = room
    room.task = asyncio.create_task(run_room(room))
    return {"room": code, "token": room.players[0].token}


async def error(ws, message):
    await ws.send_json({"type": "error", "message": message})


@app.websocket("/ws/{code}")
async def websocket_room(ws: WebSocket, code: str):
    room = rooms.get(code.upper())
    seat, player = None, None
    ws = LoggedWebSocket(ws, code.upper(),
                         lambda: room.view(seat) if room is not None and seat is not None else None)
    if not same_origin(ws.headers.get("origin"), ws.headers.get("host")):
        await ws.close(code=1008)
        return
    await ws.accept()
    if room is None:
        await error(ws, "房间不存在或已过期，请重新创建。")
        await ws.close(code=4004)
        return
    try:
        hello = await asyncio.wait_for(ws.receive_json(), timeout=10)
        # wait_for runs receive_json in a child task; adopt its correlation ID.
        audit_request_id.set(ws.request_id)
        if not isinstance(hello, dict):
            raise RuleError("连接请求格式错误。")
        token = hello.get("token")
        async with room.lock:
            if isinstance(token, str) and token:
                for i, p in room.players.items():
                    if secrets.compare_digest(p.token, token):
                        seat, player = i, p
                        break
                if player is None:
                    raise RuleError("恢复身份已失效，请返回大厅重新加入。")
            if player is None:
                name = hello.get("name", "")
                if not isinstance(name, str) or not 1 <= len(name.strip()) <= 20:
                    raise RuleError("昵称长度须为 1–20 个字符。")
                # Human joins are allowed only before dealing, to prevent mid-hand scouting.
                if room.game.phase != "lobby":
                    raise RuleError("牌局已经开始，只能由原玩家恢复连接。")
                seat = next((i for i in range(4) if i not in room.players), None)
                if seat is None:
                    raise RuleError("房间已有四位玩家。")
                player = room.players[seat] = Player(name.strip())
            was_paused = not any(p.socket for p in room.players.values())
            previous = player.socket
            player.socket = ws
            if was_paused:
                room.mark()
                room.sync_deal_clock(time.monotonic(), resume=True)
            if previous and previous is not ws:
                await previous.close(code=4001, reason="已在其他连接恢复")
            room.touched = time.monotonic()
            room.game.version += 1
            await ws.send_json({"type": "welcome", "room": room.code, "seat": seat, "token": player.token})
            await room.broadcast()
        last_action = 0.0
        while True:
            try:
                message = await ws.receive_json()
            except ValueError:
                await error(ws, "消息须为 JSON。")
                continue
            if not isinstance(message, dict):
                await error(ws, "消息格式错误。")
                continue
            async with room.lock:
                if player.socket is not ws:
                    break
                action = message.get("action")
                if not isinstance(action, str):
                    await error(ws, "缺少操作类型。")
                    continue
                # Keepalive traffic must neither consume the player's action
                # allowance nor return an action error after a recent click.
                if action == "ping":
                    await ws.send_json({"type": "pong"})
                    continue
                now = time.monotonic()
                if now - last_action < ACTION_INTERVAL:
                    await error(ws, "操作太快，请稍候。")
                    continue
                last_action = now
                try:
                    # A card arriving every 500ms must not invalidate a player's
                    # concurrent bid. Scope such commands to this exact deal;
                    # Game still validates ownership and current bid strength.
                    same_deal = (room.game.phase == "dealing"
                                 and (action in ("bid", "hint") or
                                      (action == "pass" and type(message.get("bid_revision")) is int
                                       and message["bid_revision"] == len(room.game.declarations)))
                                 and type(message.get("deal_id")) is int
                                 and message["deal_id"] == room.game.deal_id)
                    if message.get("version") != room.game.version and not same_deal:
                        await error(ws, "牌桌已更新，请按当前手牌操作。")
                        await ws.send_json(room.view(seat))
                        continue
                    if action == "hint":
                        if room.game.phase not in ("dealing", "burying", "playing") or (room.game.phase != "dealing" and seat != room.game.turn):
                            raise RuleError("轮到你时才能获取提示。")
                        ranked = room.game.ai_rankings(seat, room.ai_strategy)
                        best = ranked[0].json()
                        await ws.send_json({"type": "hint", **best, "strategy": room.ai_strategy,
                                            "alternatives": [r.json() for r in ranked[:3]],
                                            "version": room.game.version, "deal_id": room.game.deal_id})
                        continue
                    if action == "ai_strategy":
                        if seat != room.host:
                            raise RuleError("只有房主可以选择 AI 策略。")
                        if room.game.phase not in ("lobby", "round_end", "match_end"):
                            raise RuleError("请在开局前或本局结束后切换 AI 策略。")
                        chosen = get_strategy(message.get("strategy"))
                        room.ai_strategy = chosen.id
                        room.game.version += 1
                        room.game.log(f"房间 AI 已切换为{chosen.name}。")
                    elif action == "timer":
                        if seat != room.host:
                            raise RuleError("只有房主可以设置操作时限。")
                        if room.game.phase not in ("lobby", "round_end", "match_end"):
                            raise RuleError("请在开局前或本局结束后设置操作时限。")
                        seconds = message.get("seconds")
                        if ("seconds" not in message or
                                (seconds is not None and (type(seconds) is not int or seconds not in TIMER_OPTIONS))):
                            raise RuleError("请选择 15、30、60、120 秒或不限时。")
                        room.turn_seconds = seconds
                        room.game.version += 1
                        label = "不限时" if seconds is None else f"出牌 {seconds} 秒、扣底 {math.ceil(seconds * 1.5)} 秒"
                        room.game.log(f"操作时限已设为：{label}。")
                    elif action in ("start", "next", "restart"):
                        if seat != room.host:
                            raise RuleError("只有房主可以开始牌局。")
                        if action == "start":
                            if room.game.phase != "lobby":
                                raise RuleError("牌局已开始。")
                            room.game.start()
                        elif action == "next":
                            room.game.next_round()
                        else:
                            if room.game.phase != "match_end":
                                raise RuleError("打过 A 后才能开始新比赛。")
                            version = room.game.version
                            room.game = Game()
                            room.game.version = version
                            room.game.start()
                    elif action == "auto":
                        player.auto = not player.auto
                        room.game.version += 1
                    elif action == "seat":
                        target = message.get("seat")
                        if room.game.phase != "lobby" or type(target) is not int or target not in range(4):
                            raise RuleError("只能在开局前选择座位。")
                        if target in room.players:
                            raise RuleError("这个座位已经有人了。")
                        del room.players[seat]
                        room.players[target] = player
                        if room.host == seat:
                            room.host = target
                        seat = target
                        room.game.version += 1
                    else:
                        if action == "pass" and (type(message.get("bid_revision")) is not int
                                or message["bid_revision"] != len(room.game.declarations)):
                            raise RuleError("亮主已更新，请重新确认是否不亮。")
                        if (action in ("bid", "pass") and room.game.phase == "dealing"
                                and room.deal_closes_at is not None and now >= room.deal_closes_at):
                            raise RuleError("最后亮主时间已结束。")
                        room.game.act(seat, action, message.get("ids"))
                    room.sync_deal_clock(now)
                    if action != "auto" or seat == room.game.turn:
                        room.mark()
                    else:
                        room.touched = time.monotonic()
                    await room.broadcast()
                except RuleError as exc:
                    await error(ws, str(exc))
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except (RuleError, ValueError) as exc:
        audit_request_id.set(ws.request_id)
        try:
            await error(ws, str(exc))
            await ws.close(code=4003)
        except Exception:
            pass
    finally:
        audit_request_id.set(None)
        if player is not None:
            async with room.lock:
                if player.socket is ws:
                    player.socket = None
                    room.touched = time.monotonic()
                    # Transfer host controls when someone leaves; the seat itself stays reserved.
                    if seat == room.host:
                        room.host = next((i for i, p in room.players.items() if p.socket), seat)
                    room.game.version += 1
                    await room.broadcast()
