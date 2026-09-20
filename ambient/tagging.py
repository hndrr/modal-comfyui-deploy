"""Jev tagging is a separate ComfyUI job, never a prerequisite for playback."""
import asyncio
import hashlib
import json
import math
import time
from uuid import uuid4

from .urls import redirect_guard, validate_endpoint
from .split import SPLIT_HEADERS

SCHEMA = {
    "scene": {"type": "multi_choice", "instructions": "Select visual scene tags supported by the generation instructions.",
              "criteria": {k: k for k in ("forest", "water", "ocean", "mountain", "city", "interior", "abstract", "night", "day", "rain", "snow", "fog", "space")}},
    "sound": {"type": "multi_choice", "instructions": "Select audible elements requested in the sound instructions.",
              "criteria": {k: k for k in ("rain", "wind", "water", "birds", "insects", "urban", "mechanical", "music", "voices", "quiet")}},
    "motion": {"type": "score", "instructions": "How much visual movement is requested?", "criteria": ["Still", "Gentle", "Energetic"]},
    "warmth": {"type": "score", "instructions": "How warm is the visual atmosphere?", "criteria": ["Cold", "Neutral", "Warm"]},
    "dream": {"type": "score", "instructions": "How dreamlike is the scene?", "criteria": ["Literal realistic", "Atmospheric", "Surreal dream"]},
}


def recipe(state, session_id, revision):
    graph = {
        "1": {"class_type": "JevInterpret", "inputs": {"state": json.dumps(state, ensure_ascii=False),
               "state_format": "json", "schema_json": json.dumps(SCHEMA), "model": "jev-latest",
               "refresh": 0, "provider": "typesafe", "api_key": ""}},
        "2": {"class_type": "JevResolve", "inputs": {"judgments": ["1", 0], "bindings_json": "{}"}},
        "3": {"class_type": "PreviewAny", "inputs": {"source": ["2", 2]}},
    }
    return {"prompt": graph, "client_id": str(uuid4()), "extra_data": {"ambient": {
        "sessionId": session_id, "stage": "jev", "revision": revision,
        "bindings": {"state": {"node": "1", "input": "state", "source": "ambient"}}, "outputs": {"tags": "3"}}}}


async def classify(base, headers, state, session_id, revision, timeout=900):
    import aiohttp
    base = validate_endpoint(base, allow_http_loopback=not headers)
    async with aiohttp.ClientSession(headers={**headers, **SPLIT_HEADERS},
                                     timeout=aiohttp.ClientTimeout(total=120), trace_configs=[redirect_guard()]) as client:
        async def call(method, path, **kwargs):
            async with client.request(method, base + path, **kwargs) as response:
                response.raise_for_status()
                return await response.json()
        submitted = await call("POST", "/prompt", json=recipe(state, session_id, revision))
        job_id = submitted["prompt_id"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            history = (await call("GET", f"/history/{job_id}")).get(job_id)
            if history and history.get("status", {}).get("status_str") == "error":
                raise RuntimeError("Jev tagging failed; inspect its ComfyUI execution")
            if history and history.get("status", {}).get("completed"):
                execution = (await call("GET", f"/ambient/executions/{job_id}"))["executions"][0]
                output = execution["meta"]["outputs"]["tags"]
                values = json.loads(history["outputs"][output]["text"][0])
                if not isinstance(values, dict):
                    raise ValueError("Jev output must be an object")
                tags, scores = [], {}
                for kind in ("scene", "sound"):
                    selected = values.get(kind, [])
                    if not isinstance(selected, list) or any(not isinstance(tag, str) for tag in selected):
                        raise ValueError(f"Jev {kind} must be a list of tags")
                    tags.extend(f"{kind}:{tag}" for tag in selected)
                for key in ("motion", "warmth", "dream"):
                    if key in values:
                        if type(values[key]) not in (int, float) or not math.isfinite(values[key]):
                            raise ValueError(f"Jev {key} must be a finite score")
                        scores[key] = max(0, min(1, values[key]))
                # Include added schema/resolve nodes, so changing their rubrics or
                # score mappings changes the saved version as well.
                schema = {key: {"class_type": node["class_type"], "inputs": {
                    name: value for name, value in node["inputs"].items()
                    if name not in {"state", "refresh", "api_key"}}}
                    for key, node in execution["graph"].items() if node["class_type"].startswith("Jev")}
                return {"status": "completed", "tags": tags, "scores": scores, "workflowRevision": revision,
                        "schemaVersion": hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()[:16]}
            await asyncio.sleep(2)
        await call("POST", f"/jobs/{job_id}/cancel")
        raise TimeoutError("Jev tagging timed out")
