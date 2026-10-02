"""Single-writer journal. Never share a live database between containers."""

import json
import hashlib
import os
import time
import uuid
from copy import deepcopy
from pathlib import Path

ACTIVE = {"queued", "dispatching", "running", "unknown"}


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
        kind = {"completed": "execution_success", "cancelled": "execution_interrupted"}.get(
            job["status"], "execution_error")
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


def body_digest(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


class Journal:
    def __init__(self, root: Path):
        self.path = root / "controller.json"
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {
            "mode": "split", "environment": "base", "candidate": None,
            "jobs": {}, "next_number": 0, "session": None,
        }
        self.data.setdefault("retired_jobs", {})

    def save(self):
        write_json(self.path, self.data)

    def busy(self):
        return any(j["status"] in ACTIVE for j in self.data["jobs"].values())

    def enqueue(self, body, request_id=None):
        if not isinstance(body, dict):
            raise ValueError("Prompt body must be an object")
        if self.data["candidate"] or self.data["session"]:
            raise ValueError("環境更新またはモード切替中です。")
        if not isinstance(body.get("prompt"), dict) or not body["prompt"]:
            raise ValueError("prompt must be a non-empty object")
        if request_id:
            for job in self.data["retired_jobs"].values():
                if job.get("request_id") == request_id:
                    if job["body_digest"] != body_digest(body):
                        raise ValueError("Idempotency key was reused with a different prompt")
                    return job
            for job in self.data["jobs"].values():
                if job.get("request_id") == request_id:
                    if job["body"] != body:
                        raise ValueError("Idempotency key was reused with a different prompt")
                    return job
        number = self.data["next_number"]
        self.data["next_number"] += 1
        job = {
            "id": str(uuid.uuid4()), "number": -number - 1 if body.get("front") else number,
            "body": body, "environment": self.data["environment"],
            "status": "queued", "created_at": time.time(), "call_id": None,
            "request_id": request_id, "history": None, "error": None,
        }
        self.data["jobs"][job["id"]] = job
        return job

    def retire(self, ids):
        for key in ids:
            job = self.data["jobs"][key]
            if job["status"] in ACTIVE:
                raise ValueError("Cannot retire an unfinished job")
            # This compact map also lets a replayed worker reject an old input
            # after the per-job receipts have been removed.
            record = {"id": key, "number": job["number"], "status": job["status"]}
            if job.get("request_id"):
                record.update(request_id=job["request_id"], body_digest=body_digest(job["body"]))
            self.data["retired_jobs"][key] = record
            del self.data["jobs"][key]

    def queue(self):
        jobs = sorted(self.data["jobs"].values(), key=lambda j: j["number"])
        return {
            "queue_running": [prompt_record(j) for j in jobs if j["status"] in ACTIVE - {"queued"}],
            "queue_pending": [prompt_record(j) for j in jobs if j["status"] == "queued"],
        }

    def history(self):
        return {key: job_history(j) for key, j in self.data["jobs"].items() if j["history"]}

    def next_job(self):
        jobs = sorted(self.data["jobs"].values(), key=lambda j: j["number"])
        if any(j["status"] in {"dispatching", "running", "unknown"} for j in jobs):
            return None
        return next((j for j in jobs if j["status"] == "queued"), None)

    def recover(self):
        for job in self.data["jobs"].values():
            if job["status"] == "dispatching" and not job["call_id"]:
                job["status"] = "unknown"
                job["error"] = "受付後のGPU呼び出し状態を確認できません。自動再実行はしません。"

    def assert_idle(self):
        if self.busy() or self.data["candidate"] or self.data["session"]:
            raise ValueError("生成・待機ジョブ・環境更新があるため切り替えられません。")
