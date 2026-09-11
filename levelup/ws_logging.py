"""Rotating, server-only WebSocket diagnostics; never changes wire payloads."""
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import uuid


request_id = ContextVar("websocket_request_id", default=None)
logger = logging.getLogger("levelup.websocket")
logger.setLevel(logging.INFO)
logger.propagate = False


def configure():
    if any(isinstance(handler, RotatingFileHandler) for handler in logger.handlers):
        return
    directory = Path(os.environ.get("LEVELUP_LOG_DIR", Path.cwd() / "logs"))
    directory.mkdir(parents=True, exist_ok=True)
    # Separate processes must not rotate the same file.
    handler = RotatingFileHandler(directory / f"websocket-{os.getpid()}.jsonl",
                                  maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)


def redact(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if key.lower() in
                {"token", "authorization", "cookie", "password", "secret"} else redact(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class LoggedWebSocket:
    def __init__(self, socket, room, context):
        self.socket = socket
        self.room = room
        self.context = context
        self.connection_id = uuid.uuid4().hex
        self.request_id = None
        self.sequence = 0

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
        except Exception as exc:
            self.record("disconnect", error=type(exc).__name__, code=getattr(exc, "code", None))
            raise
        self.sequence += 1
        self.request_id = f"{self.connection_id}:{self.sequence}"
        request_id.set(self.request_id)
        try:
            payload = json.loads(raw)
        except ValueError:
            # Raw malformed text can contain an unparseable reconnect token.
            self.record("request", invalid_json=True, length=len(raw), context=self.context())
            raise
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
