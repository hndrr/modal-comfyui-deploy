"""Single-writer journal. Never share a live database between containers."""

import json
import os
import time
import uuid
from pathlib import Path

ACTIVE = {"queued", "dispatching", "running", "unknown"}


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

    def queue(self):
        def item(job):
            return [job["number"], job["id"], job["body"]["prompt"],
                    job["body"].get("extra_data", {}), []]
        jobs = sorted(self.data["jobs"].values(), key=lambda j: j["number"])
        return {
            "queue_running": [item(j) for j in jobs if j["status"] in ACTIVE - {"queued"}],
            "queue_pending": [item(j) for j in jobs if j["status"] == "queued"],
        }

    def history(self):
        return {key: j["history"] for key, j in self.data["jobs"].items() if j["history"]}

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
