"""Run and account for isolated ComfyUI Function/Server comparisons.

Every mutation is restricted to an Evaluation's exact names. Production Apps,
Volumes, Workers and credentials are never updated. Results contain no tokens.
"""

import argparse
import asyncio
import copy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import time
from urllib.parse import quote
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from comfy_split.evaluation import Evaluation, ui_handle


def encode(value):
    if isinstance(value, (Decimal, datetime)):
        return str(value)
    raise TypeError(type(value).__name__)


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, default=encode) + "\n")
    temporary.replace(path)


def now():
    return datetime.now(timezone.utc).isoformat()


def cli(*args, capture=False, env=None):
    result = subprocess.run([sys.executable, "-m", "modal", *args], cwd=ROOT,
                            env=env, text=True, capture_output=capture, check=True)
    return json.loads(result.stdout) if capture else None


def containers(app_id):
    return cli("container", "list", "--app-id", app_id, "--json", capture=True)


def wait_zero(app_id, timeout=300):
    started = time.monotonic()
    while containers(app_id):
        if time.monotonic() - started > timeout:
            raise TimeoutError("Evaluation containers did not scale to zero")
        time.sleep(5)
    return round(time.monotonic() - started, 3)


async def observe_containers(app_id, samples):
    """Read control-plane state without keeping the HTTP/GPU containers awake."""
    while True:
        try:
            rows = await asyncio.to_thread(containers, app_id)
            samples.append({"at": now(), "containers": rows})
        except Exception as error:
            samples.append({"at": now(), "error": type(error).__name__})
        await asyncio.sleep(3)


def deploy(evaluation, path, *, shared_models=False):
    import modal

    manifest = json.loads(path.read_text()) if path.exists() else {
        "run_id": evaluation.run_id, "backend": evaluation.backend,
        "app_name": evaluation.app_name, "started_at": now(), "deployments": [],
        "volumes": evaluation.volumes, "sdk": modal.__version__,
    }
    validate_manifest(evaluation, manifest)
    manifest["shared_models_read_only"] = shared_models
    save(path, manifest)  # Also enables exact-name cleanup after a failed build.
    env = dict(os.environ, SPLIT_EVALUATION_ID=evaluation.run_id,
               SPLIT_EVALUATION_BACKEND=evaluation.backend, COMFYUI_GPU_PROFILE="rtx-pro-6000",
               SPLIT_EVALUATION_SHARED_MODELS="1" if shared_models else "0")
    started = time.monotonic()
    cli("deploy", "--strategy", "recreate", "scripts/probe_servers.py::app", env=env)
    app = modal.App.lookup(evaluation.app_name)
    info = app.info()
    ui = ui_handle(evaluation.app_name, evaluation.backend)
    url = ui.get_url() if evaluation.backend == "server" else ui.get_web_url()
    manifest.update(app_id=info.app_id, url=url, objects={**info.functions, **info.servers})
    manifest.pop("stopped_at", None)
    manifest["storage_deleted"] = False
    manifest["deployments"].append({"finished_at": now(),
                                  "seconds": round(time.monotonic() - started, 3)})
    save(path, manifest)
    print(json.dumps({"deployed": evaluation.app_name, "url": url}), flush=True)


def validate_manifest(evaluation, manifest):
    if (manifest.get("app_name") != evaluation.app_name or
            manifest.get("volumes") != evaluation.volumes or
            manifest.get("run_id") != evaluation.run_id or
            manifest.get("backend") != evaluation.backend):
        raise ValueError("Manifest does not match the isolated evaluation names")


async def wait_ready(client, origin):
    started = time.monotonic()
    retries = 0
    while time.monotonic() - started < 600:
        async with client.get(origin + "/split/startup") as response:
            if response.status == 503:
                retries += 1
            else:
                response.raise_for_status()
                data = await response.json()
                if data.get("failed"):
                    raise RuntimeError("ComfyUI startup failed")
                if data.get("ready"):
                    return {"ready_seconds": round(time.monotonic() - started, 3),
                            "unavailable_responses": retries, "startup": data}
        await asyncio.sleep(2)
    raise TimeoutError("CPU startup exceeded 600 seconds")


async def verify_api(client, origin):
    import aiohttp
    from PIL import Image

    async def request(method, path, **kwargs):
        async with client.request(method, origin + path, **kwargs) as response:
            response.raise_for_status()
            return await response.json() if response.status != 204 else None

    objects = await request("GET", "/object_info")
    assert {"EmptyImage", "SaveImage", "KSampler"} <= objects.keys()
    extensions = await request("GET", "/extensions")
    for name in ("workflows/evaluation.json", "workflows/日本語 #?% +/動画.json",
                 "workflows/move/scene.json"):
        route = "/api/userdata/" + quote(name, safe="")
        body = {"nodes": [{"id": 1}], "evaluation": True}
        await request("POST", route + "?overwrite=true", json=body)
        assert await request("GET", route) == body
        moved = "workflows/evaluation-moved.json"
        await request("POST", route + "/move/" + quote(moved, safe=""))
        assert await request("GET", "/api/userdata/" + quote(moved, safe="")) == body
        await request("DELETE", "/api/userdata/" + quote(moved, safe=""))

    data = io.BytesIO()
    Image.frombytes("RGB", (1024, 1024), random.Random(0).randbytes(1024 * 1024 * 3)).save(data, "PNG")
    payload = data.getvalue()
    form = aiohttp.FormData()
    form.add_field("image", payload, filename="evaluation.png", content_type="image/png")
    form.add_field("overwrite", "true")
    uploaded = await request("POST", "/upload/image", data=form)
    async with client.get(origin + "/view", params={"filename": uploaded["name"],
                                                   "subfolder": uploaded["subfolder"], "type": "input"}) as response:
        response.raise_for_status()
        assert hashlib.sha256(await response.read()).digest() == hashlib.sha256(payload).digest()
    for _ in range(2):
        sid = "evaluation-" + uuid.uuid4().hex
        async with client.ws_connect(origin + "/ws", params={"clientId": sid}) as ws:
            message = await ws.receive_json(timeout=15)
            assert message["data"]["sid"] == sid
    async with client.post(origin + "/_split/restore") as response:
        assert response.status == 404
    latencies = []
    for _ in range(10):
        started = time.monotonic()
        await request("GET", "/queue")
        latencies.append(round((time.monotonic() - started) * 1000, 2))
    return {"nodes": len(objects), "extensions": len(extensions),
            "schema_hash": hashlib.sha256(json.dumps(objects, sort_keys=True).encode()).hexdigest(),
            "workflow_operations": True, "upload_bytes": len(payload),
            "websocket_reconnect": True, "internal_route_blocked": True,
            "queue_latency_ms": latencies}


async def verify_gpu(client, origin):
    async def request(method, path, **kwargs):
        async with client.request(method, origin + path, **kwargs) as response:
            response.raise_for_status()
            return await response.json()

    sid = "gpu-evaluation-" + uuid.uuid4().hex
    events = []
    ws = await client.ws_connect(origin + "/ws", params={"clientId": sid})

    async def read_events():
        async for message in ws:
            if message.type.name == "TEXT":
                events.append(json.loads(message.data))

    reader = asyncio.create_task(read_events())
    body = {"client_id": sid, "prompt": {
        "1": {"class_type": "EmptyImage", "inputs": {
            "width": 64, "height": 64, "batch_size": 1, "color": 3368601}},
        "2": {"class_type": "SaveImage", "inputs": {
            "images": ["1", 0], "filename_prefix": "evaluation/smoke"}},
    }}
    submitted_at = now()
    started = time.monotonic()
    try:
        first = await request("POST", "/prompt", json=body)
        second = await request("POST", "/prompt", json=body)
        assert first["prompt_id"] != second["prompt_id"]
        await request("POST", "/queue", json={"delete": [second["prompt_id"]]})
        # Disconnect while the cold GPU starts. No HTTP keepalive during this gap.
        await ws.close()
        await asyncio.sleep(10)
        ws = await client.ws_connect(origin + "/ws", params={"clientId": sid})
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        reader = asyncio.create_task(read_events())
        while time.monotonic() - started < 600:
            history = await request("GET", "/history/" + first["prompt_id"])
            if history:
                break
            await asyncio.sleep(2)
        else:
            raise TimeoutError("GPU smoke test exceeded 600 seconds")
        item = history[first["prompt_id"]]
        assert item["status"]["status_str"] == "success", item["status"]
        output = item["outputs"]["2"]["images"][0]
        async with client.get(origin + "/view", params=output) as response:
            response.raise_for_status()
            image = await response.read()
            assert image.startswith(b"\x89PNG")
        assert not await request("GET", "/history/" + second["prompt_id"])
        return {"submitted_at": submitted_at, "result_received_at": now(),
                "seconds": round(time.monotonic() - started, 3),
                "prompt_id": first["prompt_id"], "cancelled_id": second["prompt_id"],
                "image_sha256": hashlib.sha256(image).hexdigest(),
                "events": sorted({e["type"] for e in events}),
                "history": item, "disconnected_seconds": 10,
                "model_inference": False}
    finally:
        await ws.close()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


def model_prompt(spec, index):
    """Use a new seed each time so warm measurements still run the sampler."""
    prompt = copy.deepcopy(spec["prompt"])
    prompt[spec["seed_node"]]["inputs"]["noise_seed"] = spec["seeds"][index]
    prompt[spec["output_node"]]["inputs"]["filename_prefix"] = f"evaluation/model-{index + 1}"
    return prompt


async def verify_model(client, origin, evaluation, spec, output, records, checkpoint):
    import modal

    async def request(method, route, **kwargs):
        async with client.request(method, origin + route, **kwargs) as response:
            payload = await response.json()
            if response.status >= 400:
                raise RuntimeError(f"{method} {route}: {response.status}: {payload}")
            return payload

    sid = "model-evaluation-" + uuid.uuid4().hex
    events = []
    async with client.ws_connect(origin + "/ws", params={"clientId": sid}) as ws:
        async def receive():
            async for message in ws:
                if message.type.name == "TEXT":
                    data = json.loads(message.data)
                    events.append({"at": now(), "type": data.get("type"), "data": data.get("data")})
        reader = asyncio.create_task(receive())
        try:
            for index, seed in enumerate(spec["seeds"]):
                prompt = model_prompt(spec, index)
                record = {"index": index + 1, "seed": seed, "submitted_at": now(),
                          "prompt_sha256": hashlib.sha256(json.dumps(prompt, sort_keys=True).encode()).hexdigest()}
                records.append(record)
                checkpoint()
                started = time.monotonic()
                accepted = await request("POST", "/prompt", json={"client_id": sid, "prompt": prompt})
                prompt_id = accepted["prompt_id"]
                record["prompt_id"] = prompt_id
                checkpoint()
                while time.monotonic() - started < 850:
                    history = await request("GET", "/history/" + prompt_id)
                    if history:
                        break
                    await asyncio.sleep(2)
                else:
                    raise TimeoutError("Model inference exceeded 850 seconds")
                item = history[prompt_id]
                record["history"] = item
                checkpoint()
                if item["status"]["status_str"] != "success":
                    raise RuntimeError("Model inference failed: " + json.dumps(item["status"]))
                candidates = [value for values in item["outputs"][spec["output_node"]].values()
                              if isinstance(values, list) for value in values
                              if isinstance(value, dict) and str(value.get("filename", "")).endswith(".mp4")]
                if len(candidates) != 1:
                    raise RuntimeError("Expected one MP4 output")
                async with client.get(origin + "/view", params=candidates[0]) as response:
                    response.raise_for_status()
                    video = await response.read()
                record.update(result_received_at=now(), seconds=round(time.monotonic() - started, 3),
                              bytes=len(video), sha256=hashlib.sha256(video).hexdigest())
                destination = output.parent / f"{evaluation.backend}-seed-{seed}.mp4"
                destination.write_bytes(video)
                record["artifact"] = str(destination.resolve())
                # Read the committed GPU receipt without submitting another GPU call.
                volume = modal.Volume.from_name(evaluation.volumes["data"])
                raw = b"".join([chunk async for chunk in volume.read_file.aio("/jobs/" + prompt_id + ".json")])
                receipt = json.loads(raw)
                record["gpu_worker_seconds"] = receipt["seconds"]
                record["gpu_receipt"] = receipt
                record["events"] = [event for event in events
                                    if isinstance(event.get("data"), dict) and event["data"].get("prompt_id") == prompt_id]
                record["passed"] = True
                checkpoint()
                print(json.dumps({"model": spec["name"], "backend": evaluation.backend,
                                  "seed": seed, "seconds": record["seconds"],
                                  "gpu_worker_seconds": receipt["seconds"]}), flush=True)
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


async def check(evaluation, path, *, boots, gpu, workflow=None):
    import aiohttp
    from dotenv import dotenv_values

    manifest = json.loads(path.read_text())
    validate_manifest(evaluation, manifest)
    credentials = {**dotenv_values(ROOT / ".env"), **os.environ}
    headers = {"Modal-Key": credentials["MODAL_PROXY_KEY"],
               "Modal-Secret": credentials["MODAL_PROXY_SECRET"]}
    origin = manifest["url"].rstrip("/")
    spec = json.loads(workflow.read_text()) if workflow else None
    if spec:
        manifest["workload"] = spec
    records = manifest.setdefault("checks", [])
    for _ in range(boots):
        await asyncio.to_thread(wait_zero, manifest["app_id"])
        record = {"started_at": now()}
        records.append(record)
        save(path, manifest)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=660)) as anonymous:
            async with anonymous.get(origin + "/") as response:
                assert response.status == 401, response.status
        samples = record.setdefault("container_samples", [])
        observer = asyncio.create_task(observe_containers(manifest["app_id"], samples))
        try:
            async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=660)) as client:
                record.update(await wait_ready(client, origin))
                save(path, manifest)
                if spec:
                    model_records = record.setdefault("model_inference", [])
                    await verify_model(client, origin, evaluation, spec, path, model_records, lambda: save(path, manifest))
                else:
                    record["api"] = await verify_api(client, origin)
                    if gpu:
                        record["gpu"] = await verify_gpu(client, origin)
            record["client_closed_at"] = now()
            save(path, manifest)
            record["scale_to_zero_seconds"] = await asyncio.to_thread(wait_zero, manifest["app_id"])
            samples.append({"at": now(), "containers": []})
        finally:
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
            save(path, manifest)
        record["finished_at"] = now()
        record["passed"] = True
        save(path, manifest)
        print(json.dumps({"checked": evaluation.app_name, "ready_seconds": record["ready_seconds"],
                          "snapshot_reused": record["startup"]["snapshot"]["reused"],
                          "gpu": gpu or bool(spec), "scale_to_zero_seconds": record["scale_to_zero_seconds"]}), flush=True)


def collect(evaluation, path):
    import modal

    manifest = json.loads(path.read_text())
    validate_manifest(evaluation, manifest)
    workspace = modal.Workspace.from_context()
    rates = workspace.billing.rates()
    manifest["rates"] = {key: str(rates[key]) for key in (
        "cpu_hour_cost", "mem_gib_hour_cost", "gpu_hour_cost_rtx6000",
        "volume_storage_gib_month_cost", "egress_gib_cost")}
    start = datetime.fromisoformat(manifest["started_at"]).replace(minute=0, second=0, microsecond=0)
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    rows = workspace.billing.report(start=start, end=end, resolution="h", tag_names=["evaluation", "backend"])
    object_ids = set(manifest.get("objects", {}).values()) | {manifest.get("app_id")}
    selected = [asdict(row) for row in rows if row.object_id in object_ids or
                (row.tags.get("evaluation") == evaluation.run_id and
                 row.tags.get("backend") == evaluation.backend) or
                evaluation.app_name in row.description]
    manifest["billing"] = {"collected_at": now(), "interval_end": end.isoformat(), "rows": selected,
        "reported_cost": str(sum((row["cost"] for row in selected), Decimal(0))) if selected else None,
        "complete": False,
        "note": "Hourly reporting can lag; absent rows are unknown, not zero. Shared image build costs may be unattributed."}
    for name, object_id in manifest.get("objects", {}).items():
        handle = ui_handle(evaluation.app_name, evaluation.backend) if name == "ui" else modal.Function.from_name(evaluation.app_name, name)
        if hasattr(handle, "stats"):
            try:
                info = handle.info()
                manifest.setdefault("resources", {})[name] = {
                    "cpu": info.cpu, "memory_mib": info.memory_mib,
                    "gpus": info.gpus, "image_id": info.image_info.image_id,
                    "volumes": {mount: asdict(value) for mount, value in info.volumes.items()},
                }
                manifest.setdefault("stats", {})[name] = asdict(handle.stats(since=start))
            except Exception as error:
                manifest.setdefault("stats_errors", {})[name] = type(error).__name__ + ": " + str(error)
    save(path, manifest)
    print(json.dumps({"app": evaluation.app_name, "reported_cost": manifest["billing"]["reported_cost"]}), flush=True)


def cleanup(evaluation, path, delete_storage):
    import modal

    manifest = json.loads(path.read_text())
    validate_manifest(evaluation, manifest)
    # App names are derived, never taken from a CLI-supplied arbitrary target.
    cli("app", "stop", "--yes", evaluation.app_name)
    if manifest.get("app_id"):
        wait_zero(manifest["app_id"])
    if delete_storage:
        for name in evaluation.volumes.values():
            modal.Volume.objects.delete(name, allow_missing=True)
        for suffix in ("events", "commands"):
            modal.Queue.objects.delete(evaluation.app_name + "-" + suffix, allow_missing=True)
    manifest["stopped_at"] = now()
    manifest["storage_deleted"] = delete_storage
    save(path, manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("deploy", "check", "collect", "cleanup"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--backend", choices=("function", "server"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--boots", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--gpu", action="store_true")
    parser.add_argument("--workflow", type=Path, help="Pinned real-model benchmark specification")
    parser.add_argument("--shared-models", action="store_true", help="Mount existing weights read-only; never delete them")
    parser.add_argument("--delete-storage", action="store_true")
    args = parser.parse_args()
    os.environ["MODAL_PROFILE"] = args.profile  # Before the first modal import.
    evaluation = Evaluation(args.run_id, args.backend)
    if args.action == "deploy":
        deploy(evaluation, args.output, shared_models=args.shared_models)
    elif args.action == "check":
        try:
            asyncio.run(check(evaluation, args.output, boots=args.boots, gpu=args.gpu, workflow=args.workflow))
        except BaseException as error:
            manifest = json.loads(args.output.read_text())
            manifest.setdefault("failures", []).append({"at": now(), "action": "check",
                "error": type(error).__name__ + ": " + str(error)})
            save(args.output, manifest)
            cleanup(evaluation, args.output, False)
            raise
    elif args.action == "collect":
        collect(evaluation, args.output)
    else:
        cleanup(evaluation, args.output, args.delete_storage)


if __name__ == "__main__":
    main()
