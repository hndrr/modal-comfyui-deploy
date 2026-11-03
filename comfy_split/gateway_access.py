"""Keep the server-side MCP subprocess read-only even without its fetch wrapper."""

import re
import secrets

from aiohttp import web

HEADER = "X-Split-Gateway-Token"
ENV = "SPLIT_GATEWAY_TOKEN"


def boundary(token):
    """ASGI owns the private token; child MCP processes receive no copy of it."""
    @web.middleware
    async def check(request, handler):
        if token and secrets.compare_digest(request.headers.get(HEADER, ""), token):
            return await handler(request)
        path = request.path.removeprefix("/api") if request.path.startswith("/api/") else request.path
        if request.method in {"GET", "HEAD"} and re.fullmatch(
            r"/(?:object_info(?:/[^/]+)?|models(?:/[^/]+)?|history(?:/[^/]+)?|queue|system_stats|features|userdata(?:/.*)?|split/startup)", path
        ):
            return await handler(request)
        # The only permitted mutation is a new workflow library file. The
        # normal ComfyUI userdata endpoint still validates and saves its JSON.
        if request.method == "POST" and path.startswith("/userdata/workflows/"):
            name = path.removeprefix("/userdata/workflows/")
            raw = request.rel_url.raw_path.removeprefix("/api")
            if ("/" not in raw.removeprefix("/userdata/")
                    and 0 < len(name) <= 512 and name.lower().endswith(".json")
                    and not re.search(r"[\\:%\x00-\x1f\x7f]", name)
                    and all(p and not p.startswith((".", "~")) for p in name.split("/"))
                    and list(request.query.items()) == [("overwrite", "false")]):
                return await handler(request)
        raise web.HTTPForbidden(text="This local client may only read or save a new workflow.")
    return check
