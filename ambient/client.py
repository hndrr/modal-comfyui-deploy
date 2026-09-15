"""Small synchronous HTTP client shared by the CLI and explicit GPU smoke script."""

import json
import os
from pathlib import Path
import shutil
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from .contracts import TERMINAL, identifier, validate_request
from .urls import validate_endpoint


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError(
            "Backend redirects are not allowed; configure the final HTTPS endpoint"
        )


class Client:
    def __init__(self, base, headers, opener=None):
        self.base = validate_endpoint(base, allow_http_loopback=not headers)
        self.headers = dict(headers)
        self.opener = opener or build_opener(NoRedirect)

    @classmethod
    def from_env(cls):
        base = validate_endpoint(os.environ["AMBIENT_BACKEND_URL"])
        return cls(
            base,
            {
                "Modal-Key": os.environ["MODAL_PROXY_KEY"],
                "Modal-Secret": os.environ["MODAL_PROXY_SECRET"],
            },
        )

    def open(self, method, path, data=None, content_type=None):
        headers = dict(self.headers)
        if content_type:
            headers["Content-Type"] = content_type
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            return self.opener.open(request, timeout=120)
        except HTTPError as error:
            with error:
                detail = error.read(2048).decode(errors="replace")
            raise RuntimeError(f"Ambient HTTP {error.code}: {detail}") from error

    def call(self, method, path, data=None):
        body = json.dumps(data).encode() if data is not None else None
        with self.open(method, path, body, "application/json") as response:
            return json.load(response)

    def capabilities(self):
        return self.call("GET", "/capabilities")

    def submit(self, request):
        # Never retry with a fresh ID: callers persist and explicitly reuse this body.
        return self.call("POST", "/jobs", validate_request(request))

    def status(self, job_id):
        return self.call("GET", "/jobs/" + identifier(job_id))

    def cancel(self, job_id):
        return self.call("DELETE", "/jobs/" + identifier(job_id))

    def upload(self, path):
        boundary = "ambient-" + uuid4().hex
        with Path(path).open("rb") as source:
            data = source.read(12 * 1024 * 1024 + 1)
        prefix = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
            'filename="anchor"\r\nContent-Type: application/octet-stream\r\n\r\n'
        ).encode()
        body = prefix + data + f"\r\n--{boundary}--\r\n".encode()
        if len(body) > 12 * 1024 * 1024:
            raise ValueError("Image upload exceeds 12 MiB")
        with self.open(
            "POST", "/images", body, f"multipart/form-data; boundary={boundary}"
        ) as response:
            return identifier(json.load(response)["id"])

    def wait(self, job, timeout=3600, progress=lambda _: None, poll_interval=3):
        deadline = time.monotonic() + timeout
        while job["status"] not in TERMINAL:
            progress(job)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Inspect job {job['id']}; do not submit a new request"
                )
            time.sleep(min(poll_interval, remaining))
            job = self.status(job["id"])
        progress(job)
        return job

    def download(self, job_id, directory, job=None):
        job_id = identifier(job_id)
        job = job or self.status(job_id)
        if job["id"] != job_id or job["status"] != "completed":
            raise ValueError("Clip is not completed")
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{job_id}.mp4"
        # Use a unique temporary file so concurrent downloads cannot share a partial.
        partial = directory / f"{job_id}.{uuid4().hex}.part.mp4"
        try:
            with (
                self.open("GET", "/clips/" + job_id) as response,
                partial.open("xb") as out,
            ):
                shutil.copyfileobj(response, out, length=1024 * 1024)
            expected = job.get("clip", {}).get("bytes")
            size = partial.stat().st_size
            if size == 0 or (expected is not None and size != expected):
                raise RuntimeError(
                    "Incomplete clip download; retry download with the same job ID"
                )
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
        (directory / f"{job_id}.json").write_text(json.dumps(job, indent=2) + "\n")
        return target


def save_request(request, directory):
    """Persist the exact normalized body before POST, refusing conflicting local reuse."""
    request = validate_request(request)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{request['requestId']}.request.json"
    try:
        with target.open("x") as out:
            out.write(json.dumps(request, indent=2) + "\n")
    except FileExistsError:
        if json.loads(target.read_text()) != request:
            raise ValueError(
                "Request ID already has a different saved body; use submit with the saved file"
            )
    return target
