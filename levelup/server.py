"""SQLite-backed rooms, private player views, and serialized WebSocket actions.

Run a single worker: a room and its sockets must live in the same process.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import logging
import math
import os
import secrets
import threading
import time

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .assets import static_dir
from .legal import play_options
from .game import Game, RuleError
from .ai import DEFAULT_STRATEGY, get_strategy, strategy_catalog, observe
from .ai.candidates import generate_candidates
from .ai.search import SearchStrategy, shutdown_search
from .ai.openai_agent import OpenAIAgentStrategy
from .ws_logging import LoggedWebSocket, request_id as audit_request_id
from .security import TransportSecurity
from .storage import Store, database_path, restore_game

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
THINKING_STRATEGIES = (SearchStrategy, OpenAIAgentStrategy)


@dataclass
class Player:
    name: str
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    socket: WebSocket | None = None
    auto: bool = False
    identity: str | None = None


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
    search_task: asyncio.Task | None = None
    search_cancel: threading.Event | None = None
    search_key: tuple | None = None
    hint_tasks: dict = field(default_factory=dict)
    hint_requests: dict = field(default_factory=dict)
    play_reactions: dict = field(default_factory=dict)
    paused_at: float | None = None

    @property
    def waiting_for(self):
        return [seat for seat, p in self.players.items() if p.socket is None]

    @property
    def paused(self):
        return bool(self.waiting_for)

    def sync_presence(self):
        now = time.monotonic()
        if self.paused and self.paused_at is None:
            self.paused_at = now
            self.cancel_search()
            for cancel, task in self.hint_tasks.values():
                cancel.set()
                task.cancel()
            return True
        if not self.paused and self.paused_at is not None:
            elapsed = now - self.paused_at
            self.changed += elapsed
            self.next_draw_at += elapsed
            if self.deal_closes_at is not None:
                self.deal_closes_at += elapsed
            self.paused_at = None
            return True
        return False

    def persist(self):
        if store is not None and rooms.get(self.code) is self:
            store.save_room(self)

    def remember_reaction(self, seat, trick_number, reaction):
        # Presentation metadata stays outside the rules engine and is tied to
        # the exact game/deal/trick, so it survives reconnects without leaking
        # onto a later play by the same seat.
        from .ai.openai_agent import public_reaction
        reaction = public_reaction(reaction)
        prefix = (id(self.game), self.game.deal_id)
        self.play_reactions = {k: v for k, v in self.play_reactions.items()
                               if k[:2] == prefix and k[2] >= trick_number - 1}
        if reaction:
            self.play_reactions[(*prefix, trick_number, seat)] = reaction

    def search_signature(self):
        return (id(self.game), self.game.version, self.game.turn, self.ai_strategy)

    def cancel_search(self):
        if self.search_cancel is not None:
            self.search_cancel.set()
        if self.search_task is not None:
            self.search_task.cancel()
        self.search_task = self.search_cancel = self.search_key = None

    def search_budget(self):
        remaining = self.timeout()
        if remaining is not None:
            remaining = max(0, remaining - (time.monotonic() - self.changed) - .5)
        return 28.0 if remaining is None else min(28.0, remaining)

    def mark(self):
        self.cancel_search()
        for cancel, task in self.hint_tasks.values():
            cancel.set()
            task.cancel()
        self.touched = time.monotonic()
        self.changed = self.paused_at if self.paused_at is not None else self.touched

    def automated(self, seat):
        p = self.players.get(seat)
        return p is None or p.auto

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

    def auto_confirm_bids(self):
        changed = False
        for seat in range(4):
            if seat not in self.game.bid_passed and self.game.bid_skip_reason(seat):
                self.game.act(seat, "pass")
                changed = True
        return changed

    def advance_dealing(self, now):
        """Keep dealing independent of human actions, AI pauses and turn timers."""
        game = self.game
        if game.phase != "dealing" or self.paused:
            return False
        self.sync_deal_clock(now)
        changed = self.auto_confirm_bids()
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
        changed = self.auto_confirm_bids() or changed
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
        changed = self.auto_confirm_bids() or changed
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
        def with_reaction(play, number):
            reaction = self.play_reactions.get((id(game), game.deal_id, number, play['seat']))
            return {**play, **({'reaction': reaction} if reaction else {})}
        state['trick'] = [with_reaction(p, len(game.history) + 1) for p in state['trick']]
        if state['last_trick']:
            last = state['last_trick']
            state['last_trick'] = {**last, 'plays': [with_reaction(p, last['number']) for p in last['plays']]}
        state["play_options"] = play_options(
            game.rules, game.hands[seat], game.trick[0][1] if game.trick else ()
        ) if game.phase == "playing" and game.turn == seat else None
        state.update({"type": "state", "room": self.code, "seat": seat, "host": self.host,
                      "paused": self.paused, "waiting_for": self.waiting_for,
                      "turn_seconds": self.turn_seconds,
                      "bury_seconds": None if self.turn_seconds is None else math.ceil(self.turn_seconds * 1.5),
                      "timer_options": list(TIMER_OPTIONS),
                      "ai_strategy": self.ai_strategy, "ai_strategies": web_strategy_catalog(),
                      "ai_thinking": self.search_task is not None and self.search_key == self.search_signature(),
                      "draw_interval": DRAW_INTERVAL,
                      "deal_close_seconds": DEAL_CLOSE_SECONDS,
                      "deal_closing": self.game.phase == "dealing" and self.deal_closes_at is not None,
                      "deal_seconds_left": max(0, math.ceil(self.deal_closes_at - (self.paused_at if self.paused_at is not None else time.monotonic())))
                          if self.game.phase == "dealing" and self.deal_closes_at is not None else None,
                      "players": [{"seat": i, "name": p.name if p else f"AI · {'南东北西'[i]}",
                                   "human": p is not None, "connected": bool(p and p.socket),
                                   "auto": self.automated(i)} for i in range(4)
                                  for p in [self.players.get(i)]],
                      "seconds_left": None if self.timeout() is None else
                          max(0, round(self.timeout() - ((self.paused_at if self.paused_at is not None else time.monotonic()) - self.changed)))})
        return state

    def timeout(self):
        if self.turn_seconds is None:
            return None
        return math.ceil(self.turn_seconds * 1.5) if self.game.phase == "burying" else self.turn_seconds

    async def broadcast(self):
        self.sync_presence()
        self.persist()  # Commit before clients observe a successful state transition.
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
store: Store | None = None


def get_room(code):
    if code in rooms:
        return rooms[code]
    data = store.load_room(code) if store is not None else None
    if data is None or len(rooms) >= MAX_ROOMS:
        return None
    room = Room(code, game=restore_game(data["game"]), host=data["host"],
                ai_strategy=data["ai_strategy"], turn_seconds=data["turn_seconds"])
    try:
        web_strategy(room.ai_strategy)
    except RuleError:
        room.ai_strategy = DEFAULT_STRATEGY
        room.game.log("原 AI 策略当前未开放，已恢复为默认记牌策略。")
    room.players = {p["seat"]: Player(p["name"], token=p["token"], auto=p["auto"], identity=p["identity"])
                    for p in data["players"]}
    clock = data["clock"]
    now = time.monotonic()
    room.paused_at = now
    room.changed = now - clock["elapsed"]
    room.next_draw_at = now + clock["draw_wait"]
    room.deal_closes_at = None if clock["close_wait"] is None else now + clock["close_wait"]
    room.dealing_id, room.closing_bid_count = clock["dealing_id"], clock["closing_bid_count"]
    room.play_reactions = {(id(room.game), deal, trick, seat): reaction
                           for deal, trick, seat, reaction in data.get("reactions", [])}
    rooms[code] = room
    room.task = asyncio.create_task(run_room(room))
    return room


async def search_ranking(context, strategy, seconds, cancel):
    # Both slow policies work on immutable player-visible snapshots, outside the
    # room lock. Remote I/O is async; CPU search uses its shared process pool.
    def work():
        return strategy.search(context, generate_candidates(context), seconds=seconds, cancel=cancel)
    try:
        if isinstance(strategy, OpenAIAgentStrategy):
            async def decide():
                candidates = await asyncio.to_thread(generate_candidates, context)
                return await strategy.decide(context, candidates, seconds=seconds, cancel=cancel)
            return await asyncio.wait_for(decide(), timeout=seconds + .2)
        return await asyncio.wait_for(asyncio.to_thread(work), timeout=seconds + .2)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        cancel.set()
        raise


def consume_task_error(task):
    if not task.cancelled():
        task.exception()  # A discarded stale task must not emit an unhandled exception.


async def search_hint(room, seat, ws, context, key, strategy, seconds, cancel):
    try:
        result = await search_ranking(context, strategy, seconds, cancel)
        async with room.lock:
            if (room.paused or room.search_signature() != key or cancel.is_set()
                    or room.players.get(seat) is None or room.players[seat].socket is not ws):
                return
            ranked = result.ranked
            await ws.send_json({'type': 'hint', **ranked[0].json(), 'strategy': strategy.id,
                               'alternatives': [r.json() for r in ranked[:3]],
                               ('agent' if isinstance(strategy, OpenAIAgentStrategy) else 'search'): result.report,
                               'version': room.game.version,
                               'deal_id': room.game.deal_id})
    except asyncio.CancelledError:
        cancel.set()
        raise
    except Exception:
        log.exception('AI hint failed: %s', room.code)
        async with room.lock:
            if room.search_signature() == key and room.players[seat].socket is ws:
                await error(ws, 'AI 提示暂不可用，请手动出牌。')
    finally:
        if room.hint_tasks.get(seat, (None, None))[1] is asyncio.current_task():
            room.hint_tasks.pop(seat, None)


async def run_room(room):
    try:
        while rooms.get(room.code) is room:
            await asyncio.sleep(ROOM_TICK)
            async with room.lock:
                presence_changed = room.sync_presence()
                connected = any(p.socket for p in room.players.values())
                if room.paused or not connected:
                    room.cancel_search()
                    for cancel, task in room.hint_tasks.values():
                        cancel.set()
                        task.cancel()
                    if presence_changed:
                        await room.broadcast()
                    if not connected and time.monotonic() - room.touched > ROOM_TTL:
                        room.persist()
                        rooms.pop(room.code, None)
                        return
                    continue  # Pause abandoned rooms instead of burning CPU playing AI rounds.
                game = room.game
                if room.search_task is not None and room.search_key != room.search_signature():
                    room.cancel_search()
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
                strategy = get_strategy(room.ai_strategy)
                reaction = ''
                if game.phase == 'playing' and isinstance(strategy, THINKING_STRATEGIES) and automatic:
                    if room.search_task is None:
                        room.search_cancel = threading.Event()
                        room.search_key = room.search_signature()
                        room.search_task = asyncio.create_task(search_ranking(
                            observe(game, game.turn), strategy, room.search_budget(), room.search_cancel))
                        room.search_task.add_done_callback(consume_task_error)
                        await room.broadcast()
                        continue
                    if not room.search_task.done():
                        continue
                    try:
                        result = room.search_task.result()
                        chosen = result.ranked[0].action
                        action, ids = chosen.kind, [c.id for c in chosen.cards]
                        if isinstance(strategy, OpenAIAgentStrategy):
                            reaction = result.report.get('reaction', '') if result.report.get('status') == 'selected' else ''
                            # Server-only diagnostics correlated with the exact
                            # turn; private tactical reasons are not broadcast.
                            for player in room.players.values():
                                if isinstance(player.socket, LoggedWebSocket):
                                    player.socket.record('agent_decision', version=game.version,
                                                         deal_id=game.deal_id, seat=game.turn,
                                                         ids=ids, agent=result.report)
                                    break
                    except Exception:
                        log.exception('AI failed, using rule-based fallback: %s', room.code)
                        reaction = ''
                        action, ids = game.ai_action(game.turn, 'rule_based')
                    room.cancel_search()
                else:
                    # An expired human clock must not start another slow request.
                    policy = 'rule_based' if isinstance(strategy, THINKING_STRATEGIES) else room.ai_strategy
                    action, ids = game.ai_action(game.turn, policy)
                if not automatic:
                    game.log(f"{game.turn + 1} 号位操作超时，AI 代行本次操作。")
                seat, trick_number = game.turn, len(game.history) + 1
                game.act(seat, action, ids)
                if action == 'play':
                    room.remember_reaction(seat, trick_number, reaction)
                room.mark()
                await room.broadcast()
    except asyncio.CancelledError:
        room.cancel_search()
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
    global store
    store = Store(database_path())
    yield
    tasks = [r.task for r in rooms.values() if r.task]
    for room in rooms.values():
        room.cancel_search()
        for cancel, task in room.hint_tasks.values():
            cancel.set()
            task.cancel()
            tasks.append(task)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for room in rooms.values():
        for player in room.players.values():
            player.socket = None
        room.sync_presence()
        room.persist()
    rooms.clear()
    shutdown_search()
    store.close()
    store = None


app = FastAPI(title="升级 · Levelup", lifespan=lifespan)
app.add_middleware(TransportSecurity)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class CreateRoom(BaseModel):
    name: str = Field(min_length=1, max_length=20)
    ai_strategy: str = DEFAULT_STRATEGY
    identity: str | None = Field(default=None, max_length=128)


class GuestIdentity(BaseModel):
    identity: str | None = Field(default=None, max_length=128)
    name: str | None = Field(default=None, max_length=20)


@app.post("/api/identity")
async def guest_identity(body: GuestIdentity, request: Request):
    if not same_origin(request.headers.get("origin"), request.headers.get("host")):
        raise HTTPException(403, "请从游戏页面恢复身份。")
    try:
        secret, profile = store.identity(body.identity, body.name.strip() if body.name is not None else None)
    except RuleError as exc:
        raise HTTPException(401, str(exc)) from exc
    from fastapi.responses import JSONResponse
    return JSONResponse({"identity": secret, "name": profile["name"]}, headers={"Cache-Control": "no-store"})


@app.get("/api/me/rooms")
async def pending_rooms(request: Request):
    authorization = request.headers.get("authorization", "")
    try:
        profile = store.profile(authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else None)
    except RuleError as exc:
        raise HTTPException(401, str(exc)) from exc
    from fastapi.responses import JSONResponse
    return JSONResponse({"name": profile["name"], "rooms": store.pending(profile["identity"])},
                        headers={"Cache-Control": "no-store"})


@app.get("/")
async def index():
    # Always fetch the entry point so reloads pick up the current asset versions.
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/health")
async def health():
    return {"status": "ok", "rooms": len(rooms)}


@app.get("/api/ai-strategies")
async def ai_strategies():
    return {"default": DEFAULT_STRATEGY, "strategies": web_strategy_catalog()}


def web_strategy_catalog():
    configured = os.environ.get('LEVELUP_WEB_AI_STRATEGIES')
    allowed = None if configured is None else {s.strip() for s in configured.split(',')}
    return [s for s in strategy_catalog() if allowed is None or s['id'] in allowed or s['id'] == DEFAULT_STRATEGY]


def web_strategy(strategy_id):
    strategy = get_strategy(strategy_id)
    if strategy.id not in {s['id'] for s in web_strategy_catalog()}:
        raise RuleError("服务器未开放此 AI 策略。")
    return strategy


def same_origin(origin, host):
    from urllib.parse import urlsplit
    if origin is None:
        return True  # CLI clients do not send Origin; this is not authentication.
    try:
        parsed = urlsplit(origin)
        return (parsed.scheme in ('http', 'https') and bool(parsed.hostname)
                and parsed.netloc == host and not parsed.username and not parsed.password
                and not parsed.path and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


@app.post("/api/rooms")
async def create_room(body: CreateRoom, request: Request):
    if not same_origin(request.headers.get("origin"), request.headers.get("host")):
        raise HTTPException(403, "请从游戏页面创建房间。")
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "请输入昵称。")
    try:
        web_strategy(body.ai_strategy)
    except RuleError as exc:
        raise HTTPException(422, str(exc)) from exc
    if len(rooms) >= MAX_ROOMS:
        raise HTTPException(503, "房间已满，请稍后再试。")
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    while code in rooms or store.has_room(code):
        code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    try:
        secret, profile = store.identity(body.identity, name)
    except RuleError as exc:
        raise HTTPException(401, str(exc)) from exc
    room = Room(code, ai_strategy=body.ai_strategy)
    room.players[0] = Player(name, identity=profile["identity"])
    rooms[code] = room
    room.sync_presence()
    room.persist()
    room.task = asyncio.create_task(run_room(room))
    from fastapi.responses import JSONResponse
    return JSONResponse({"room": code, "token": room.players[0].token, "identity": secret, "name": name},
                        headers={"Cache-Control": "no-store"})


async def error(ws, message):
    await ws.send_json({"type": "error", "message": message})


@app.websocket("/ws/{code}")
async def websocket_room(ws: WebSocket, code: str):
    room = None
    seat, player = None, None
    ws = LoggedWebSocket(ws, code.upper(),
                         lambda: room.view(seat) if room is not None and seat is not None else None)
    if not same_origin(ws.headers.get("origin"), ws.headers.get("host")):
        await ws.close(code=1008)
        return
    await ws.accept()
    room = get_room(code.upper())
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
        if token is not None and (not isinstance(token, str) or len(token) > 128 or not token.isascii()):
            raise RuleError("恢复身份格式错误。")
        identity = hello.get("identity")
        profile = store.profile(identity) if identity is not None else None
        if "name" in hello and (not isinstance(hello["name"], str) or not 1 <= len(hello["name"].strip()) <= 20):
            raise RuleError("昵称长度须为 1–20 个字符。")
        async with room.lock:
            if profile:
                for i, p in room.players.items():
                    if p.identity == profile["identity"]:
                        seat, player = i, p
                        break
            if player is None and isinstance(token, str) and token:
                for i, p in room.players.items():
                    if secrets.compare_digest(p.token, token):
                        seat, player = i, p
                        break
                if player is None:
                    raise RuleError("恢复身份已失效，请返回大厅重新加入。")
                if profile and player.identity != profile["identity"]:
                    raise RuleError("此座位不属于当前访客身份。")
            if player is None:
                name = hello.get("name", profile["name"] if profile else "")
                if not isinstance(name, str) or not 1 <= len(name.strip()) <= 20:
                    raise RuleError("昵称长度须为 1–20 个字符。")
                # Human joins are allowed only before dealing, to prevent mid-hand scouting.
                if room.game.phase != "lobby":
                    raise RuleError("牌局已经开始，只能由原玩家恢复连接。")
                seat = next((i for i in range(4) if i not in room.players), None)
                if seat is None:
                    raise RuleError("房间已有四位玩家。")
                if profile is None:
                    identity, profile = store.identity(name=name.strip())
                player = room.players[seat] = Player(name.strip(), identity=profile["identity"])
            if profile is None:
                # Compatibility with existing per-room recovery tokens: migrate
                # that authorized seat to a persistent browser identity.
                identity, profile = store.identity(name=player.name)
                player.identity = profile["identity"]
            supplied_name = hello.get("name", player.name)
            if not isinstance(supplied_name, str) or not 1 <= len(supplied_name.strip()) <= 20:
                raise RuleError("昵称长度须为 1–20 个字符。")
            player.name = supplied_name.strip()
            store.identity(identity, player.name)
            previous = player.socket
            player.socket = ws
            room.sync_presence()
            room.sync_deal_clock(room.paused_at if room.paused_at is not None else time.monotonic())
            if previous and previous is not ws:
                await previous.close(code=4001, reason="已在其他连接恢复")
            room.touched = time.monotonic()
            room.game.version += 1
            room.persist()
            await ws.send_json({"type": "welcome", "room": room.code, "seat": seat, "token": player.token,
                               "identity": identity, "name": player.name})
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
                    if room.paused and action not in ("kick", "ai_strategy", "timer", "seat", "auto"):
                        raise RuleError("牌桌已暂停，需等待所有真人重新加入；房主可移除未归队的玩家。")
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
                        strategy = get_strategy(room.ai_strategy)
                        if room.game.phase == 'playing' and isinstance(strategy, THINKING_STRATEGIES):
                            hint_key = (id(room.game), room.game.deal_id, len(room.game.history), len(room.game.trick))
                            if room.hint_requests.get(seat) == hint_key:
                                raise RuleError("本次出牌已请求过 AI 提示，请等待结果或手动出牌。")
                            room.hint_requests[seat] = hint_key
                            previous_hint = room.hint_tasks.get(seat)
                            if previous_hint is not None:
                                previous_hint[0].set()
                                previous_hint[1].cancel()
                            cancel = threading.Event()
                            task = asyncio.create_task(search_hint(
                                room, seat, ws, observe(room.game, seat), room.search_signature(),
                                strategy, room.search_budget(), cancel))
                            task.add_done_callback(consume_task_error)
                            room.hint_tasks[seat] = (cancel, task)
                            await ws.send_json({'type': 'hint_pending', 'version': room.game.version})
                            continue
                        ranked = room.game.ai_rankings(seat, room.ai_strategy)
                        best = ranked[0].json()
                        await ws.send_json({"type": "hint", **best, "strategy": room.ai_strategy,
                                            "alternatives": [r.json() for r in ranked[:3]],
                                            "version": room.game.version, "deal_id": room.game.deal_id})
                        continue
                    if action == "kick":
                        if seat != room.host:
                            raise RuleError("只有原房主可以移除玩家。")
                        target = message.get("seat")
                        if type(target) is not int or target == seat or target not in room.players:
                            raise RuleError("请选择其他真人玩家。")
                        removed = room.players.pop(target)
                        previous_socket, removed.socket = removed.socket, None
                        room.game.version += 1
                        room.game.log(f"房主移除了 {removed.name}，该座位由 AI 接管。")
                        room.sync_presence()
                        room.persist()
                        if previous_socket:
                            await previous_socket.close(code=4005, reason="房主已移除该座位")
                    elif action == "ai_strategy":
                        if seat != room.host:
                            raise RuleError("只有房主可以选择 AI 策略。")
                        if room.game.phase not in ("lobby", "round_end", "match_end"):
                            raise RuleError("请在开局前或本局结束后切换 AI 策略。")
                        chosen = web_strategy(message.get("strategy"))
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
                    room.sync_deal_clock(room.paused_at if room.paused_at is not None else now)
                    if action != "kick" and (action != "auto" or seat == room.game.turn):
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
                    hint = room.hint_tasks.get(seat)
                    if hint is not None:
                        hint[0].set()
                        hint[1].cancel()
                    player.socket = None
                    room.touched = time.monotonic()
                    # The starter retains ownership across disconnects/restarts.
                    room.game.version += 1
                    await room.broadcast()
