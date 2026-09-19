"""Streaming HTTP/WebSocket proxy used by the CPU and the private GPU tunnel."""

import asyncio

from aiohttp import WSMsgType, web

HOP = {"host", "connection", "upgrade", "transfer-encoding", "content-length",
       "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
       "sec-websocket-accept", "origin", "authorization", "modal-key", "modal-secret"}


async def proxy(request, client, origin, token=None, body=None, before_response=None,
                *, path=None, sockets=None):
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    if token:
        headers["Authorization"] = "Bearer " + token
    url = origin.rstrip("/") + (path or request.rel_url.raw_path_qs)
    if request.headers.get("Upgrade", "").lower() == "websocket":
        async with client.ws_connect(url, headers=headers, compress=0, max_msg_size=0) as upstream:
            socket = web.WebSocketResponse(compress=False, max_msg_size=0)
            await socket.prepare(request)
            if sockets is not None:
                sockets.add(socket)

            async def relay(source, destination):
                async for message in source:
                    if message.type == WSMsgType.TEXT:
                        await destination.send_str(message.data)
                    elif message.type == WSMsgType.BINARY:
                        await destination.send_bytes(message.data)
                    elif message.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                        break

            tasks = [asyncio.create_task(relay(socket, upstream)),
                     asyncio.create_task(relay(upstream, socket))]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await socket.close()
                if sockets is not None:
                    sockets.discard(socket)
            return socket
    async with client.request(request.method, url, headers=headers,
                              data=body if body is not None else request.content if request.can_read_body else None,
                              allow_redirects=False) as upstream:
        response_headers = {k: v for k, v in upstream.headers.items()
                            if k.lower() not in HOP}
        location = response_headers.get("Location", "")
        if location.startswith(origin):
            response_headers["Location"] = location[len(origin):] or "/"
        response = web.StreamResponse(status=upstream.status, headers=response_headers)
        if before_response is not None and upstream.status < 400:
            await before_response()
        await response.prepare(request)
        async for chunk in upstream.content.iter_chunked(256 * 1024):
            await response.write(chunk)
        await response.write_eof()
        return response
