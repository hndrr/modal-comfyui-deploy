"""Small, read-only snapshots of native ComfyUI execution events."""
import math
import time


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def observe(job, kind, data):
    if not isinstance(data, dict) or data.get("prompt_id") not in (None, job["id"]):
        return
    if kind not in ("executing", "progress", "progress_state"):
        return
    if kind == "progress_state":
        nodes = data.get("nodes")
        if not isinstance(nodes, dict):
            return
        for node, state in nodes.items():
            if isinstance(state, dict) and state.get("state") == "running":
                observe(job, "progress", {"node": node, "value": state.get("value"), "max": state.get("max")})
        return
    prev = job.get("live_progress") or {}
    node = data.get("node", prev.get("node"))
    if node is None or not isinstance(node, (str, int)):
        return
    node = str(node)[:256]
    if kind == "executing" or prev.get("node") != node:
        prev = {"node": node}
    if kind == "progress" and number(data.get("value")) and number(data.get("max")) and 0 <= data["value"] <= data["max"] and data["max"] > 0:
        prev.update(value=data["value"], max=data["max"])
    job["live_progress"] = prev


def snapshot(job, now=None):
    now = time.time() if now is None else now
    history = job.get("history") or {}
    messages = (history.get("status") or {}).get("messages", [])
    start = end = error = None
    for kind, data in messages:
        if not isinstance(data, dict):
            continue
        if kind == "execution_start":
            start = data.get("timestamp")
        if kind in ("execution_success", "execution_error", "execution_interrupted"):
            end = data.get("timestamp")
        if kind == "execution_error":
            error = {k: str(data[k])[:1200] for k in ("node_id", "node_type", "exception_type", "exception_message") if data.get(k)}
    if error is None and job.get("error"):
        error = {"exception_message": str(job["error"])[:1200]}
    terminal = job["status"] in {"completed", "failed", "cancelled"}
    elapsed_end = job.get("finished_at") if terminal else now
    return {
        "prompt_id": job["id"], "status": job["status"],
        "cancel_requested": bool(job.get("cancel_requested")),
        "progress": job.get("live_progress") if job["status"] == "running" else None,
        "elapsed_seconds": round(max(0, elapsed_end-job["created_at"]), 3) if number(elapsed_end) and number(job.get("created_at")) else None,
        "execution_seconds": round((end-start)/1000, 3) if number(start) and number(end) and end >= start else None,
        "worker_seconds": job.get("seconds"), "error": error,
    }
