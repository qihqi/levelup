"""Versioned JSON game snapshots and guest memberships in SQLite; no pickle."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

from .game import Card, Game, RuleError

GAME_FIELDS = ("phase", "levels", "dealer", "round", "version", "dealt", "deal_id",
               "last_draw_seat", "trump", "level", "turn", "bid", "declarations",
               "last_trick", "score", "history", "result", "events")


def game_snapshot(game):
    def cards(hand):
        return [[c.id, c.suit, c.rank] for c in hand]
    return {**{name: getattr(game, name) for name in GAME_FIELDS},
            "hands": [cards(h) for h in game.hands], "bottom": cards(game.bottom),
            "draw_pile": cards(game.draw_pile),
            "trick": [[seat, cards(hand)] for seat, hand in game.trick],
            "bid_passed": sorted(game.bid_passed), "rng": game.rng.getstate()}


def restore_game(data):
    def cards(hand):
        return [Card(*c) for c in hand]
    def tuples(value):
        return tuple(map(tuples, value)) if isinstance(value, list) else value
    game = Game()
    for name in GAME_FIELDS:
        setattr(game, name, data[name])
    game.hands = [cards(h) for h in data["hands"]]
    game.bottom, game.draw_pile = cards(data["bottom"]), cards(data["draw_pile"])
    game.trick = [(seat, cards(hand)) for seat, hand in data["trick"]]
    game.bid_passed = set(data["bid_passed"])
    game.rng.setstate(tuples(data["rng"]))
    return game


def database_path():
    root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return Path(os.environ.get("LEVELUP_DB_PATH", root / "levelup" / "state.sqlite3")).expanduser()


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS profiles (
                identity TEXT PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rooms (
                code TEXT PRIMARY KEY, snapshot TEXT NOT NULL,
                phase TEXT NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memberships (
                room TEXT NOT NULL REFERENCES rooms(code) ON DELETE CASCADE,
                identity TEXT NOT NULL REFERENCES profiles(identity), seat INTEGER NOT NULL,
                PRIMARY KEY(room, identity), UNIQUE(room, seat)
            );
            CREATE INDEX IF NOT EXISTS memberships_identity ON memberships(identity);
        """)

    def close(self):
        self.db.close()

    def profile(self, secret):
        if not isinstance(secret, str) or len(secret) != 43 or not secret.isascii():
            raise RuleError("访客身份无效，请重新打开大厅。")
        identity = hashlib.sha256(secret.encode()).hexdigest()
        row = self.db.execute("SELECT identity, name FROM profiles WHERE identity=?", (identity,)).fetchone()
        if row is None:
            raise RuleError("访客身份已失效，请重置本机身份后重新加入。")
        return dict(row)

    def identity(self, secret=None, name=None):
        if secret:
            profile = self.profile(secret)
            if name is not None:
                with self.db:
                    self.db.execute("UPDATE profiles SET name=? WHERE identity=?", (name, profile["identity"]))
                profile["name"] = name
        else:
            secret = secrets.token_urlsafe(32)
            profile = {"identity": hashlib.sha256(secret.encode()).hexdigest(), "name": name or ""}
            with self.db:
                self.db.execute("INSERT INTO profiles VALUES (?, ?, ?)",
                                (profile["identity"], profile["name"], time.time()))
        return secret, profile

    def save_room(self, room):
        now = room.paused_at if room.paused_at is not None else time.monotonic()
        snapshot = {"schema": 1, "game": game_snapshot(room.game), "host": room.host,
                    "ai_strategy": room.ai_strategy, "turn_seconds": room.turn_seconds,
                    "players": [{"seat": seat, "name": p.name, "token": p.token,
                                 "identity": p.identity, "auto": p.auto}
                                for seat, p in room.players.items()],
                    "clock": {"elapsed": max(0, now - room.changed),
                              "draw_wait": max(0, room.next_draw_at - now),
                              "close_wait": None if room.deal_closes_at is None else
                                  max(0, room.deal_closes_at - now),
                              "dealing_id": room.dealing_id,
                              "closing_bid_count": room.closing_bid_count},
                    "reactions": [[deal, trick, seat, reaction]
                                  for (game_id, deal, trick, seat), reaction in room.play_reactions.items()
                                  if game_id == id(room.game)]}
        payload = json.dumps(snapshot, ensure_ascii=False)
        with self.db:
            self.db.execute("""INSERT INTO rooms VALUES (?, ?, ?, ?) ON CONFLICT(code)
                DO UPDATE SET snapshot=excluded.snapshot, phase=excluded.phase, updated_at=excluded.updated_at""",
                            (room.code, payload, room.game.phase, time.time()))
            self.db.execute("DELETE FROM memberships WHERE room=?", (room.code,))
            self.db.executemany("INSERT INTO memberships VALUES (?, ?, ?)",
                                [(room.code, p.identity, seat) for seat, p in room.players.items()
                                 if p.identity is not None])

    def load_room(self, code):
        row = self.db.execute("SELECT snapshot FROM rooms WHERE code=?", (code,)).fetchone()
        if row is None:
            return None
        data = json.loads(row["snapshot"])
        if data.get("schema") != 1:
            raise ValueError("Unsupported saved room schema")
        return data

    def has_room(self, code):
        return self.db.execute("SELECT 1 FROM rooms WHERE code=?", (code,)).fetchone() is not None

    def pending(self, identity):
        rows = self.db.execute("""SELECT r.code, r.snapshot, r.updated_at, m.seat
            FROM memberships m JOIN rooms r ON r.code=m.room
            WHERE m.identity=? AND r.phase != 'match_end' ORDER BY r.updated_at DESC""", (identity,))
        result = []
        for row in rows:
            data = json.loads(row["snapshot"])
            game = data["game"]
            result.append({"room": row["code"], "seat": row["seat"], "host": data["host"],
                           "phase": game["phase"], "round": game["round"], "levels": game["levels"],
                           "updated_at": row["updated_at"],
                           "players": [{"seat": p["seat"], "name": p["name"]} for p in data["players"]]})
        return result
