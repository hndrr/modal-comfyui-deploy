"""Reset CPU-only transient caches after a Modal memory restoration."""

async def restore(request):
    import random
    import secrets
    import folder_paths
    import numpy
    import torch
    from aiohttp import web
    from server import PromptServer
    from .cpu_guard import cpu_guard_enabled

    server = PromptServer.instance
    running, pending = server.prompt_queue.get_current_queue()
    if running or pending or not cpu_guard_enabled():
        raise web.HTTPConflict(text="CPU snapshot is not idle or guarded")
    folder_paths.filename_list_cache.clear()
    random.seed(None)
    numpy.random.seed(None)
    torch.manual_seed(secrets.randbits(63))
    return web.json_response({"cpu_guard": True})
