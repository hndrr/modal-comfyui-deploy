"""Jev tagging is a separate ComfyUI job, never a prerequisite for playback."""
import asyncio
import hashlib
import json
import math
import re
import time
from uuid import uuid4

from .urls import redirect_guard, validate_endpoint
from .split import SPLIT_HEADERS

SCHEMA = {
    "scene": {"type": "multi_choice", "instructions": "Select visual scene tags supported by the generation instructions.",
              "criteria": {k: k for k in ("forest", "water", "ocean", "mountain", "city", "interior", "abstract", "night", "day", "rain", "snow", "fog", "space", "waveform", "lissajous", "glitch", "electronic", "neon", "feedback")}},
    "sound": {"type": "multi_choice", "instructions": "Select audible elements requested in the sound instructions.",
              "criteria": {k: k for k in ("rain", "wind", "water", "birds", "insects", "urban", "mechanical", "music", "voices", "quiet", "guitar", "percussion", "drums", "bass", "electronic", "sine", "shoegaze", "uk_garage", "glitch")}},
    "motion": {"type": "score", "instructions": "How much visual movement is requested?", "criteria": ["Still", "Gentle", "Energetic"]},
    "warmth": {"type": "score", "instructions": "How warm is the visual atmosphere?", "criteria": ["Cold", "Neutral", "Warm"]},
    "dream": {"type": "score", "instructions": "How dreamlike is the scene?", "criteria": ["Literal realistic", "Atmospheric", "Surreal dream"]},
}


def recipe(state, session_id, revision):
    # Current Jev exposes one judgment per Interpret node and returns its resolved
    # value directly. One shared state input keeps every judgment in sync when a
    # saved workflow receives the next generation's effective settings.
    graph = {"1": {"class_type": "PrimitiveStringMultiline", "inputs": {
        "value": json.dumps(state, ensure_ascii=False)}}}
    outputs = {}
    for index, (name, field) in enumerate(SCHEMA.items()):
        node, preview = str(2 + index * 2), str(3 + index * 2)
        candidates = str(100 + index)
        graph[candidates] = {"class_type": "PrimitiveStringMultiline", "inputs": {
            "value": json.dumps(list(field["criteria"]))}}
        graph[node] = {"class_type": "JevInterpret", "inputs": {
            "state": ["1", 0], "instructions": field["instructions"], "task": field["type"],
            "candidates_json": [candidates, 0],
            "model": "jev-latest", "refresh": 0, "provider": "typesafe", "api_key": ""}}
        graph[preview] = {"class_type": "PreviewAny", "inputs": {"source": [node, 0]}}
        outputs[name] = preview
    return {"prompt": graph, "client_id": str(uuid4()), "extra_data": {"ambient": {
        "sessionId": session_id, "stage": "jev", "revision": revision,
        "bindings": {"state": {"node": "1", "input": "value", "source": "ambient"}}, "outputs": outputs}}}


class TaggingError(RuntimeError):
    def __init__(self, message, execution_id):
        super().__init__(message)
        self.execution_id = execution_id


def execution_error(history, job_id):
    # Remote errors can contain API responses and input values. Expose only a
    # recognized missing-node name; keep the execution ID for inspection.
    for kind, data in history.get("status", {}).get("messages", []):
        if kind == "execution_error":
            missing = re.search(r"Node '([A-Za-z0-9_ -]{1,80})' not found", str(data.get("exception_message", "")))
            if missing:
                return TaggingError(f"Auto-tagging requires an unavailable ComfyUI node: {missing[1]}.", job_id)
    return TaggingError("Auto-tagging failed. Inspect this Jev execution in ComfyUI.", job_id)


def schema_version(graph):
    schema = {}
    def visit(node_id):
        if node_id in schema:
            return
        node = graph[node_id]
        inputs = {name: value for name, value in node["inputs"].items()
                  if name not in {"state", "refresh", "api_key"}}
        schema[node_id] = {"class_type": node["class_type"], "inputs": inputs}
        for value in inputs.values():
            if isinstance(value, list):
                visit(str(value[0]))
    for key, node in graph.items():
        if node["class_type"].startswith("Jev"):
            visit(key)
    return hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()[:16]


def result(history, execution):
    contract = execution["meta"]["outputs"]
    def value(output):
        return json.loads(history["outputs"][output]["text"][0])
    # Retain the result contract for accepted jobs using a legacy saved graph.
    values = value(contract["tags"]) if "tags" in contract else {
        name: value(contract[name]) for name in SCHEMA}
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
    return {"status": "completed", "tags": tags, "scores": scores,
            "workflowRevision": execution["meta"]["revision"],
            "schemaVersion": schema_version(execution["graph"])}


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
                raise execution_error(history, job_id)
            if history and history.get("status", {}).get("completed"):
                execution = (await call("GET", f"/ambient/executions/{job_id}"))["executions"][0]
                return {**result(history, execution), "executionId": job_id}
            await asyncio.sleep(2)
        await call("POST", f"/jobs/{job_id}/cancel")
        raise TimeoutError("Jev tagging timed out")
