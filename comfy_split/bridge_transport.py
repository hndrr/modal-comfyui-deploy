"""Negotiated HTTP payloads; WebSocket references preserve message ordering."""

import json
import io
import secrets
import time

from aiohttp import ClientTimeout, WSMessage, WSMsgType, web

PREFIX = "/agent_runtime/bridge"
TRANSPORT = "http-v1"
THRESHOLD = 256 * 1024
MAX_MESSAGE = 20 * 1024 * 1024
MAX_BUFFER = 64 * 1024 * 1024


def envelope(raw):
    # Only tiny transport references are control messages. Large native JSON
    # doesn't need to be parsed just to decide whether to fetch a payload.
    if len(raw) > 1024:
        return None
    value = json.loads(raw)
    return value.get("id") if isinstance(value, dict) and value.get("type") == "bridge_payload" else None


class HttpChannel:
    def __init__(self, socket, session, enabled):
        self.socket, self.session, self.enabled = socket, session, enabled
        self.incoming, self.outgoing = {}, {}

    def clear(self):
        self.session = None
        self.incoming.clear()
        self.outgoing.clear()

    def store(self, bucket, raw):
        size = len(raw.encode("utf-8"))
        if size > MAX_MESSAGE:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_MESSAGE, actual_size=size)
        now = time.monotonic()
        for mapping in (self.incoming, self.outgoing):
            for key, (_, _, created) in list(mapping.items()):
                if now - created > 120:
                    mapping.pop(key, None)
        values = [item for mapping in (self.incoming, self.outgoing) for item in mapping.values()]
        if len(values) >= 32 or sum(item[1] for item in values) + size > MAX_BUFFER:
            raise web.HTTPTooManyRequests(text="Bridge payload buffer is full")
        key = secrets.token_hex(16)
        bucket[key] = (raw, size, now)
        return key

    async def send_str(self, raw):
        if self.enabled and len(raw.encode("utf-8")) > THRESHOLD:
            key = self.store(self.outgoing, raw)
            await self.socket.send_json({"type": "bridge_payload", "id": key})
        else:
            await self.socket.send_str(raw)

    def decode(self, raw):
        key = envelope(raw) if self.enabled else None
        if key is None:
            return raw
        if key not in self.incoming:
            raise ValueError("Unknown or expired Bridge payload")
        return self.incoming.pop(key)[0]

    def authorize_session(self, request):
        if not self.enabled or not self.session or self.socket.closed or request.query.get("session") != self.session:
            raise web.HTTPConflict(text="Bridge transport session has ended")

    async def http(self, request):
        self.authorize_session(request)
        path = request.path.removeprefix("/api")
        if request.method == "POST" and path == PREFIX + "/messages":
            chunks, size = [], 0
            async for chunk in request.content.iter_chunked(256 * 1024):
                size += len(chunk)
                if size > MAX_MESSAGE:
                    raise web.HTTPRequestEntityTooLarge(max_size=MAX_MESSAGE, actual_size=size)
                chunks.append(chunk)
            self.authorize_session(request)  # A disconnect can happen during upload.
            try:
                raw = b"".join(chunks).decode("utf-8")
            except UnicodeDecodeError:
                raise web.HTTPBadRequest(text="Expected UTF-8 Bridge JSON") from None
            return web.json_response({"id": self.store(self.incoming, raw)})
        if request.method == "GET" and path.startswith(PREFIX + "/messages/"):
            key = path.rsplit("/", 1)[-1]
            item = self.outgoing.pop(key, None)
            if item is not None and time.monotonic() - item[2] <= 120:
                return web.Response(text=item[0], content_type="application/json")
        raise web.HTTPNotFound()


class HttpSocket:
    """Client-side wrapper used on the CPU -> GPU connection."""
    def __init__(self, socket, client, url, token, session):
        self.socket, self.client, self.url = socket, client, url
        self.headers = {"Authorization": "Bearer " + token}
        self.params = {"session": session}

    @property
    def closed(self):
        return self.socket.closed

    async def close(self):
        return await self.socket.close()

    async def send_json(self, data):
        await self.send_str(json.dumps(data))

    async def send_str(self, raw):
        if len(raw.encode("utf-8")) <= THRESHOLD:
            return await self.socket.send_str(raw)
        async with self.client.post(self.url + PREFIX + "/messages", params=self.params,
                headers=self.headers, data=io.BytesIO(raw.encode("utf-8")), timeout=ClientTimeout(total=60)) as response:
            response.raise_for_status()
            key = (await response.json())["id"]
        await self.socket.send_json({"type": "bridge_payload", "id": key})

    async def __aiter__(self):
        async for message in self.socket:
            key = envelope(message.data) if message.type == WSMsgType.TEXT else None
            if key is not None:
                async with self.client.get(self.url + PREFIX + "/messages/" + key,
                        params=self.params, headers=self.headers, timeout=ClientTimeout(total=60)) as response:
                    response.raise_for_status()
                    message = WSMessage(WSMsgType.TEXT, await response.text(), "")
            yield message
