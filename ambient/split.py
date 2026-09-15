"""Require the CPU gateway before using ComfyUI's native APIs."""

SPLIT_HEADERS = {"X-Modal-Execution-Mode": "split"}


async def check_split(client, base: str) -> dict:
    async with client.get(base + "/modal-control/v1/status") as response:
        if response.status != 200:
            raise RuntimeError(
                "AMBIENT_COMFYUI_URL must point to the splitapp CPU UI endpoint "
                f"(control API returned HTTP {response.status})"
            )
        state = await response.json()
    if state.get("api_version") != 1 or state.get("mode") != "split":
        raise RuntimeError("Ambient H3 requires splitapp in split mode; disable legacy mode")
    if state.get("transitioning") or state.get("candidate"):
        raise RuntimeError("Finish the splitapp environment update or mode transition first")
    if state.get("unknown_jobs"):
        raise RuntimeError("Resolve splitapp's unknown jobs before submitting Ambient H3")
    return state
