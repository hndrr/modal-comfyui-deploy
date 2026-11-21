"""ComfyUI Manager's candidate installation queue, shared by UI and MCP."""
from aiohttp import ClientTimeout


async def read_queue(client, origin, *, timeout):
    async with client.get(origin + "/v2/manager/queue/status", timeout=ClientTimeout(total=timeout)) as response:
        response.raise_for_status()
        if response.status != 200:
            raise ValueError("Manager queue status could not be verified")
        state = await response.json()
    if not isinstance(state, dict) or "is_processing" not in state:
        raise ValueError("Unrecognized Manager queue status")
    return {key: state.get(key) for key in ("is_processing", "pending_count", "in_progress_count")}


def busy(state):
    return bool(state["is_processing"] or state["pending_count"])
