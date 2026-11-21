"""Adapt ComfyUI execution events and history for Split workers, clients and stored jobs."""
import math
import time
from copy import deepcopy


TERMINAL_EVENTS = {
    "completed": "execution_success",
    "cancelled": "execution_interrupted",
    "failed": "execution_error",
}


def terminal_kind(status):
    return TERMINAL_EVENTS.get(status, TERMINAL_EVENTS["failed"])


def history_status(history):
    status = history.get("status", {})
    if any(message[0] == TERMINAL_EVENTS["cancelled"] for message in status.get("messages", [])):
        return "cancelled"
    return "completed" if status.get("status_str") == "success" else "failed"


def is_execution_end(event):
    return event.get("type") == "executing" and event.get("data", {}).get("node") is None


def prompt_record(job, original=None):
    """Supply native job metadata without changing the idempotent request body."""
    body = job.get("body", {})
    prompt = list(original or [job.get("number", 0), job["id"], body.get("prompt", {}), {}, []])
    extra = dict(body.get("extra_data") or {})
    extra.update(prompt[3] or {})
    if extra.get("create_time") is None:
        extra["create_time"] = int(job["created_at"] * 1000)
    prompt[3] = extra
    return prompt


def job_history(job):
    """Also repair old controller-created failures at the native API boundary."""
    history = deepcopy(job.get("history") or {})
    history["prompt"] = prompt_record(job, history.get("prompt"))
    history.setdefault("outputs", {})
    if not history.get("status"):
        kind = terminal_kind(job["status"])
        history["status"] = {
            "status_str": "success" if job["status"] == "completed" else "error",
            "completed": job["status"] == "completed",
            "messages": [[kind, {"prompt_id": job["id"],
                "timestamp": int(job.get("finished_at", job["created_at"]) * 1000),
                **({"exception_message": str(job.get("error") or "Execution failed")}
                   if kind == "execution_error" else {})}]],
        }
    for kind, detail in history["status"].get("messages", []):
        if kind == "execution_error":
            defaults = {"prompt_id": job["id"], "node_id": "", "node_type": "",
                        "executed": [], "exception_type": "RemoteExecutionError",
                        "exception_message": str(job.get("error") or "Execution failed"),
                        "traceback": [], "current_inputs": {}, "current_outputs": []}
            for key, value in defaults.items():
                detail.setdefault(key, value)
    return history


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
        if kind in TERMINAL_EVENTS.values():
            end = data.get("timestamp")
        if kind == "execution_error":
            error = {k: str(data[k])[:1200] for k in ("node_id", "node_type", "exception_type", "exception_message") if data.get(k)}
    if error is None and job.get("error"):
        error = {"exception_message": str(job["error"])[:1200]}
    terminal = job["status"] in TERMINAL_EVENTS
    elapsed_end = job.get("finished_at") if terminal else now
    return {
        "prompt_id": job["id"], "status": job["status"],
        "gpu": job.get("gpu"), "gpu_reason": job.get("gpu_reason"),
        "cancel_requested": bool(job.get("cancel_requested")),
        "progress": job.get("live_progress") if job["status"] == "running" else None,
        "elapsed_seconds": round(max(0, elapsed_end-job["created_at"]), 3) if number(elapsed_end) and number(job.get("created_at")) else None,
        "execution_seconds": round((end-start)/1000, 3) if number(start) and number(end) and end >= start else None,
        "worker_seconds": job.get("seconds"), "error": error,
    }


def live_events(record, data):
    """Yield live UI notifications and retain output frames until their files are durable."""
    kind = data.get("type") if isinstance(data, dict) else None
    if kind:
        observe(record, kind, data.get("data"))
    # Terminal notifications are reconstructed from durable history
    # in finish(), after output reload and journal persistence.
    if kind in TERMINAL_EVENTS.values():
        return
    # Files referenced in executed events may not be committed yet.
    if kind == "executed":
        record.setdefault("deferred_events", []).append(data)
    else:
        yield data
    # Frontend's Total counter uses `executed`/`execution_cached`,
    # while core reports non-output node completion in progress_state.
    # Emit one empty UI result only for those genuinely finished
    # prompt nodes; leave real UI results and expanded child IDs alone.
    if kind in {"executed", "execution_cached", "progress_state"}:
        detail = data.get("data", {})
        notified = record.setdefault("completed_nodes", [])
        if kind == "executed":
            notified.append(str(detail["node"]))
        elif kind == "execution_cached":
            notified.extend(str(node) for node in detail.get("nodes", []))
        elif detail.get("prompt_id") == record["id"]:
            prompt = record.get("body", {}).get("prompt", {})
            for node_id, node in detail.get("nodes", {}).items():
                if node.get("state") != "finished" or node_id not in prompt or node_id in notified:
                    continue
                notified.append(node_id)
                yield {"type": "executed", "data": {
                    "prompt_id": record["id"], "node": node_id,
                    "display_node": node.get("display_node_id", node_id), "output": {},
                }}


def finished_events(record, deferred):
    """Yield saved outputs and completion after the caller has persisted the history."""
    delivered = set()
    for event in deferred:
        if record["status"] != "completed" and str(event["data"]["node"]) not in record["history"]["outputs"]:
            continue
        yield event
        delivered.add(str(event["data"]["node"]))
    # Queue overflow/disconnection must not lose saved node previews.
    for node_id, output in record["history"]["outputs"].items():
        if node_id not in delivered:
            meta = record["history"].get("meta", {}).get(node_id, {})
            yield {"type": "executed", "data": {
                "prompt_id": record["id"], "node": node_id,
                "display_node": meta.get("display_node", node_id), "output": output,
            }}
    terminal = terminal_kind(record["status"])
    messages = record["history"].get("status", {}).get("messages", [])
    detail = next((data for kind, data in reversed(messages) if kind == terminal),
                  {"prompt_id": record["id"], "timestamp": int(record["finished_at"] * 1000)})
    yield {"type": terminal, "data": detail}
    yield {"type": "executing", "data": {"node": None, "prompt_id": record["id"]}}
