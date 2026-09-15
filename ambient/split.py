"""Require the CPU gateway before using ComfyUI's native APIs."""

SPLIT_HEADERS = {"X-Modal-Execution-Mode": "split"}


def check_dependencies(state: dict, mode: str) -> None:
    if mode != "fasth3":
        return
    kitchen = state.get("dependencies", {}).get("comfy-kitchen", {})
    if (not kitchen.get("version") or kitchen.get("version") != kitchen.get("expected")
            or kitchen.get("missingApis") != []):
        raise ValueError(
            "FastH3 requires the ComfyUI-pinned comfy-kitchen and VSA/INT8 APIs; "
            "rebuild splitapp and check its active environment"
        )


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
