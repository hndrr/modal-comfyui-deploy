"""Preserve encoded ComfyUI file paths at the Modal ASGI boundary."""

from urllib.parse import quote


def encoded_path(scope, *, has_body=False):
    raw_path = scope.get("raw_path")
    if raw_path:
        return raw_path.decode("ascii")
    # Modal 1.1.4's ASGI serialization omits raw_path too. Restore ComfyUI's
    # route parameter boundaries before forwarding the decoded path. Keep the
    # query separate, including any literal ?/# that belongs to a file name.
    path = scope["path"]
    for prefix in ("/api/userdata/", "/userdata/"):
        if path.startswith(prefix):
            file = path[len(prefix):]
            if scope.get("method") == "POST" and not has_body and "/move/" in file:
                source, dest = file.rsplit("/move/", 1)
                return prefix + quote(source, safe="") + "/move/" + quote(dest, safe="")
            return prefix + quote(file, safe="")
    return quote(path, safe="/")


def web_server_proxy(host, port):
    # Modal 1.1.4's web_server proxy reconstructs URLs from ASGI's decoded
    # `path`. ComfyUI's /userdata/{file} requires folders to stay encoded in
    # that single route parameter. Keep the pinned SDK's streaming, lifespan
    # and WebSocket handling, but supply the original wire path to its proxy.
    from modal._runtime.asgi import web_server_proxy as modal_proxy

    upstream = modal_proxy(host, port)

    async def app(scope, receive, send):
        if scope["type"] in {"http", "websocket"}:
            has_body = False
            if (not scope.get("raw_path") and scope.get("method") == "POST"
                    and scope["path"].startswith(("/userdata/", "/api/userdata/"))
                    and "/move/" in scope["path"]):
                # ComfyUI saves send file contents; moves send no body. This
                # also permits saving workflows inside a folder named "move"
                # after Modal has erased the encoded slash boundaries.
                first = await receive()
                has_body = bool(first.get("body") or first.get("more_body"))
                original_receive = receive

                async def replay():
                    nonlocal first
                    if first is not None:
                        message, first = first, None
                        return message
                    return await original_receive()

                receive = replay
            scope = {**scope, "path": encoded_path(scope, has_body=has_body)}
        await upstream(scope, receive, send)

    return app
