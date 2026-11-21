"""Render external queue snapshots with ComfyUI's own job serializer.

Read-only compatibility dependency: comfy_execution.jobs. No queue instance or
class is replaced. This module is the sole boundary for the upstream job schema.
"""
from aiohttp import web


def serialize_jobs(body):
    """Return the public HTTP payload and status for a split queue snapshot."""
    from comfy_execution.jobs import JobStatus, get_all_jobs, get_job

    running = [item[:5] for item in body["queue"]["queue_running"]]
    queued = [item[:5] for item in body["queue"]["queue_pending"]]
    history = body["history"]
    if body.get("job_id"):
        job = get_job(body["job_id"], running, queued, history)
        return (job, 200) if job else ({"error": "Job not found"}, 404)
    query = body.get("query", {})
    try:
        statuses = [s.strip().lower() for s in query.get("status", "").split(",") if s.strip()] or None
        if statuses and any(s not in JobStatus.ALL for s in statuses):
            raise ValueError("Invalid job status")
        sort_by = query.get("sort_by", "created_at").lower()
        sort_order = query.get("sort_order", "desc").lower()
        if sort_by not in {"created_at", "execution_duration"} or sort_order not in {"asc", "desc"}:
            raise ValueError("Invalid job sort")
        limit = int(query["limit"]) if "limit" in query else None
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        offset = max(0, int(query.get("offset", 0)))
    except (TypeError, ValueError) as error:
        return {"error": str(error)}, 400
    items, total = get_all_jobs(running, queued, history, status_filter=statuses,
                               workflow_id=query.get("workflow_id"), sort_by=sort_by,
                               sort_order=sort_order, limit=limit, offset=offset)
    return {"jobs": items, "pagination": {
        "offset": offset, "limit": limit, "total": total, "has_more": offset + len(items) < total}}, 200


async def jobs(request):
    payload, status = serialize_jobs(await request.json())
    return web.json_response(payload, status=status)
