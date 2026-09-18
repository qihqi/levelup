"""Rotating, server-only WebSocket diagnostics; never changes wire payloads."""
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import time
import uuid

from starlette.websockets import WebSocketDisconnect


request_id = ContextVar("websocket_request_id", default=None)
logger = logging.getLogger("levelup.websocket")
logger.setLevel(logging.INFO)
logger.propagate = False
MAX_MESSAGE_BYTES = 16384
MAX_JSON_DEPTH = 20
MESSAGE_BURST = 40
MESSAGES_PER_SECOND = 10


class PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        # The opener also applies after rotation; do not depend on the process umask.
        def opener(path, flags):
            fd = os.open(path, flags, 0o600)
            os.fchmod(fd, 0o600)
            return fd
        return open(self.baseFilename, self.mode, encoding=self.encoding, opener=opener)


def configure():
    if any(isinstance(handler, RotatingFileHandler) for handler in logger.handlers):
        return
    directory = Path(os.environ.get("LEVELUP_LOG_DIR", Path.cwd() / "logs"))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Separate processes must not rotate the same file.
    handler = PrivateRotatingFileHandler(directory / f"websocket-{os.getpid()}.jsonl",
                                  maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)


def redact(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if re.sub(r"[^a-z0-9]", "", key.lower()) in
                {"token", "identity", "authorization", "cookie", "password", "secret", "apikey", "openaiapikey",
                 "accesstoken", "refreshtoken", "clientsecret", "setcookie", "proxyauthorization"} else redact(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        # Also catch recognizable credentials accidentally pasted into a name or model output.
        return re.sub(r"\b(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}|"
                      r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})",
                      "[REDACTED]", value)
    return value


class LoggedWebSocket:
    def __init__(self, socket, room, context):
        self.socket = socket
        self.room = room
        self.context = context
        self.connection_id = uuid.uuid4().hex
        self.request_id = None
        self.sequence = 0
        self.message_tokens = float(MESSAGE_BURST)
        self.last_message = time.monotonic()

    def __getattr__(self, name):
        return getattr(self.socket, name)

    def record(self, event, **fields):
        try:
            configure()
            logger.info(json.dumps(redact({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": event, "room": self.room, "connection_id": self.connection_id,
                "request_id": request_id.get(), **fields,
            }), ensure_ascii=False, separators=(",", ":")))
        except Exception:
            # Diagnostics must not interrupt a game if storage becomes unavailable.
            logging.getLogger("levelup").exception("Cannot write WebSocket diagnostics")

    async def receive_json(self):
        request_id.set(None)
        try:
            # Read frames ourselves so malformed JSON is counted and correlated too.
            raw = await self.socket.receive_text()
        except KeyError:
            await self.close(code=1003, reason="只支持文本 JSON")
            raise WebSocketDisconnect(code=1003) from None
        except Exception as exc:
            self.record("disconnect", error=type(exc).__name__, code=getattr(exc, "code", None))
            raise
        now = time.monotonic()
        self.message_tokens = min(MESSAGE_BURST, self.message_tokens + (now - self.last_message) * MESSAGES_PER_SECOND)
        self.last_message = now
        if self.message_tokens < 1 or len(raw.encode('utf-8')) > MAX_MESSAGE_BYTES:
            code = 1008 if self.message_tokens < 1 else 1009
            await self.close(code=code, reason="消息过快或过大")
            raise WebSocketDisconnect(code=code)
        self.message_tokens -= 1
        self.sequence += 1
        self.request_id = f"{self.connection_id}:{self.sequence}"
        request_id.set(self.request_id)
        try:
            payload = json.loads(raw)
            pending = [(payload, 0)]
            while pending:
                item, depth = pending.pop()
                if depth > MAX_JSON_DEPTH:
                    raise ValueError("JSON 嵌套过深。")
                if isinstance(item, (dict, list)):
                    pending.extend((v, depth + 1) for v in (item.values() if isinstance(item, dict) else item))
        except (ValueError, RecursionError):
            # Raw malformed text can contain an unparseable reconnect token.
            self.record("request", invalid_json=True, length=len(raw), context=self.context())
            raise ValueError("消息须为有效且嵌套有限的 JSON。") from None
        self.record("request", payload=payload, context=self.context())
        return payload

    async def send_json(self, data):
        # Record before yielding to the transport, so a received response is on disk.
        self.record("response", payload=data)
        try:
            await self.socket.send_json(data)
        except BaseException as exc:
            self.record("send_failed", message_type=data.get("type"), error=type(exc).__name__)
            raise

    async def close(self, code=1000, reason=None):
        self.record("close", code=code, reason=reason)
        await self.socket.close(code=code, reason=reason)
