"""Small transport limits; public deployments still need an authenticated TLS proxy."""
import asyncio
from collections import deque
import time

from starlette.responses import JSONResponse

MAX_HTTP_BODY = 8192
ROOM_CREATIONS_PER_MINUTE = 10
MAX_RATE_CLIENTS = 4096
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                               "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


class RoomCreationLimiter:
    def __init__(self):
        self.clients = {}

    def allow(self, peer):
        now = time.monotonic()
        self.clients = {key: times for key, times in self.clients.items() if times[-1] > now - 60}
        if peer not in self.clients:
            if len(self.clients) >= MAX_RATE_CLIENTS:
                return False
            self.clients[peer] = deque()
        times = self.clients[peer]
        while times and times[0] <= now - 60:
            times.popleft()
        if len(times) >= ROOM_CREATIONS_PER_MINUTE:
            return False
        times.append(now)
        return True


class TransportSecurity:
    def __init__(self, app):
        self.app = app
        self.room_limiter = RoomCreationLimiter()

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)

        async def secured_send(message):
            if message['type'] == 'http.response.start':
                headers = list(message.get('headers', []))
                headers.extend((k.lower().encode(), v.encode()) for k, v in SECURITY_HEADERS.items())
                message = {**message, 'headers': headers}
            await send(message)

        if scope['method'] == 'POST' and scope['path'] == '/api/rooms':
            peer = (scope.get('client') or ('unknown', 0))[0]
            if not self.room_limiter.allow(peer):
                return await JSONResponse({'detail': '创建房间过快，请稍后再试。'}, 429,
                                          headers={'Retry-After': '60'})(scope, receive, secured_send)
            body = bytearray()
            try:
                async with asyncio.timeout(10):
                    while True:
                        message = await receive()
                        if message['type'] == 'http.disconnect':
                            return
                        body.extend(message.get('body', b''))
                        if len(body) > MAX_HTTP_BODY:
                            return await JSONResponse({'detail': '请求过大。'}, 413)(scope, receive, secured_send)
                        if not message.get('more_body', False):
                            break
            except TimeoutError:
                return await JSONResponse({'detail': '请求超时。'}, 408)(scope, receive, secured_send)

            original_receive = receive
            delivered = False

            async def bounded_receive():
                nonlocal delivered
                if delivered:
                    return await original_receive()
                delivered = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}

            receive = bounded_receive
        await self.app(scope, receive, secured_send)
