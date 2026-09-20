"""Isolated ComfyUI CPU Memory Snapshot experiment; never deploys the gateway.

Deploy with: scripts/modal.sh deploy scripts/probe_cpu_snapshot.py
Compare with: MODAL_PROFILE=tarotieee .venv/bin/python scripts/probe_cpu_snapshot.py
Stop with: scripts/modal.sh app stop comfyui-cpu-snapshot-probe

Uses the deployed image and an immutable environment mounted read-only. All
user files and outputs stay in the disposable container. No GPU, provider
secrets, generation requests, or public HTTP endpoint are configured.
"""

import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

import modal

APP_NAME = "comfyui-cpu-snapshot-probe"
IMAGE_ID = "im-BUQdzUJzuhhjHZzFg6a8yl"
ENVIRONMENT = "env-cd487277be974529ac2a0969e52cedac"
READY_FILE = Path("/tmp/comfy-snapshot-probe-ready.json")
HELPER_LOG = Path("/tmp/comfy-snapshot-probe-helper.log")
COMFY_LOG = Path("/tmp/split-comfy-cpu.log")
ORIGIN = "http://127.0.0.1:8187"
REQUIRED_CLASSES = {
    "AgentRuntimeBridgeImageGen", "AgentRuntimeBridgeMedia", "AgentRuntimeBridgeText",
    "BasicGuider", "BasicScheduler", "BlockSparseAttention", "CLIPLoader", "CreateVideo",
    "JevInterpret", "KSamplerSelect", "LoadImage", "LoraLoaderModelOnly", "ManualSigmas",
    "MiniMaxH3FastVAEDecode", "MiniMaxH3ImageToVideo", "MiniMaxH3SigmaShift",
    "ModelAttentionBackend", "PreviewAny", "PreviewImage", "PrimitiveStringMultiline",
    "RandomNoise", "SamplerCustomAdvanced", "SaveVideo", "UNETLoader", "VAEDecodeAudio",
    "VAELoader",
}

# Keep the same ComfyUI subprocess and interpreter as production, with an
# isolated user directory. The helper has no Controller, dispatcher or Queue.
HELPER = r"""
import asyncio, json, os, sys, time
from pathlib import Path
from comfy_split import runtime
runtime.USER = Path('/tmp/comfy-snapshot-probe-user')
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
os.environ['SPLIT_APP'] = 'comfyui-cpu-snapshot-probe'
async def main():
    process = runtime.ComfyProcess('cpu', 8187)
    try:
        started = time.monotonic()
        await process.start(sys.argv[1], cpu=True)
        Path(sys.argv[2]).write_text(json.dumps({
            'comfyInitSeconds': time.monotonic() - started,
            'comfyPid': process.process.pid,
            'environment': process.version,
        }))
        await asyncio.Event().wait()
    finally:
        await process.stop()
asyncio.run(main())
"""

app = modal.App(APP_NAME)
image = modal.Image.from_id(IMAGE_ID)
resources = dict(
    image=image,
    cpu=2,
    memory=8192,
    min_containers=0,
    max_containers=1,
    scaledown_window=2,
    startup_timeout=300,
    timeout=60,
    retries=0,
    block_network=True,
    volumes={
        "/environments": modal.Volume.from_name("comfy-split-environments").read_only(),
    },
)


class _ComfyProbe:
    def start_comfy(self):
        self.initialization_id = uuid.uuid4().hex
        self.initial_container_id = os.environ.get("MODAL_TASK_ID")
        self.initialized_at = time.time()
        self.inspections = 0
        READY_FILE.unlink(missing_ok=True)
        started = time.monotonic()
        with HELPER_LOG.open("ab") as log:
            self.helper = subprocess.Popen(
                [sys.executable, "-u", "-c", HELPER, ENVIRONMENT, str(READY_FILE)],
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
        while time.monotonic() - started < 270:
            if self.helper.poll() is not None:
                raise RuntimeError("ComfyUI helper failed: " + HELPER_LOG.read_text()[-6000:])
            if READY_FILE.exists():
                self.initialization = json.loads(READY_FILE.read_text())
                self.initialization["totalInitSeconds"] = time.monotonic() - started
                print(json.dumps({"event": "probe_initialized", "id": self.initialization_id,
                                  **self.initialization}), flush=True)
                return
            time.sleep(0.25)
        raise TimeoutError("ComfyUI initialization exceeded 270 seconds")

    def after_restore(self):
        # These values must be fresh on every boot, including snapshot restores.
        self.restoration_id = uuid.uuid4().hex
        self.container_id = os.environ.get("MODAL_TASK_ID")
        self.restored_at = time.time()
        print(json.dumps({"event": "probe_ready", "initializationId": self.initialization_id,
                          "restorationId": self.restoration_id,
                          "containerId": self.container_id}), flush=True)

    async def inspect_comfy(self):
        import hashlib
        import aiohttp

        if self.helper.poll() is not None:
            raise RuntimeError("ComfyUI helper exited after restore")
        started = time.monotonic()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as client:
            async def read(path):
                async with client.get(ORIGIN + path) as response:
                    response.raise_for_status()
                    return await response.json()

            objects, catalog, queue, extensions = await asyncio.gather(*[
                read(path) for path in ("/object_info", "/_split/catalog", "/queue", "/extensions")
            ])
            assert catalog["cpu_guard"], "CPU generation guard is missing"
            assert not catalog["import_failures"], catalog["import_failures"]
            assert not (REQUIRED_CLASSES - objects.keys()), sorted(REQUIRED_CLASSES - objects.keys())
            assert not queue["queue_running"] and not queue["queue_pending"], queue
            async with client.get(ORIGIN + "/") as response:
                response.raise_for_status()
                assert len(await response.read()) > 100, "ComfyUI frontend is missing"
            client_id = "snapshot-probe-" + uuid.uuid4().hex
            async with client.ws_connect(ORIGIN + "/ws", params={"clientId": client_id}) as ws:
                message = await ws.receive_json(timeout=10)
                assert message["type"] == "status", message
                assert message["data"]["sid"] == client_id, message

        self.inspections += 1
        return {
            "initializationId": self.initialization_id,
            "initialContainerId": self.initial_container_id,
            "restorationId": self.restoration_id,
            "containerId": self.container_id,
            "initializedAt": self.initialized_at,
            "restoredAt": self.restored_at,
            "inspectionsInContainer": self.inspections,
            **self.initialization,
            "healthCheckSeconds": time.monotonic() - started,
            "nodeCount": len(objects),
            "schemaHash": hashlib.sha256(json.dumps(objects, sort_keys=True).encode()).hexdigest(),
            "geminiNodeCount": sum(name.startswith("GeminiTools") for name in objects),
            "extensionsCount": len(extensions),
            "cpuGuard": True, "websocket": True, "queueEmpty": True,
            "importSummary": [line for line in COMFY_LOG.read_text(errors="replace").splitlines()
                              if "seconds:" in line and "ComfyUI-" in line],
        }

    def stop_comfy(self):
        # Modal terminates the container tree too; explicitly stop Comfy first.
        pid = getattr(self, "initialization", {}).get("comfyPid")
        if pid:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        helper = getattr(self, "helper", None)
        if helper and helper.poll() is None:
            helper.terminate()
            try:
                helper.wait(timeout=5)
            except subprocess.TimeoutExpired:
                helper.kill()
                helper.wait(timeout=5)


@app.cls(**resources, enable_memory_snapshot=True)
class SnapshotProbe(_ComfyProbe):
    @modal.enter(snap=True)
    def initialize(self):
        self.start_comfy()

    @modal.enter(snap=False)
    def restore(self):
        self.after_restore()

    @modal.method()
    async def inspect(self):
        return await self.inspect_comfy()

    @modal.exit()
    def stop(self):
        self.stop_comfy()


@app.cls(**resources)
class ColdProbe(_ComfyProbe):
    @modal.enter()
    def initialize(self):
        self.start_comfy()
        self.after_restore()

    @modal.method()
    async def inspect(self):
        return await self.inspect_comfy()

    @modal.exit()
    def stop(self):
        self.stop_comfy()


async def compare(output: Path, max_snapshot_boots: int):
    output.mkdir(parents=True, exist_ok=True)
    reports = []
    initialization_ids = set()
    confirmed_restores = 0
    schema_hash = None
    deadline = time.monotonic() + 1200

    async def wait_idle(method):
        until = time.monotonic() + 90
        while time.monotonic() < until:
            stats = await method.get_current_stats.aio()
            if stats.num_total_runners == 0 and stats.backlog == 0:
                return
            await asyncio.sleep(3)
        raise TimeoutError("Probe did not scale to zero between cold boots")

    for kind, count in (("ColdProbe", 2), ("SnapshotProbe", max_snapshot_boots)):
        probe = modal.Cls.from_name(APP_NAME, kind)()
        for index in range(count):
            if time.monotonic() > deadline:
                raise TimeoutError("Snapshot experiment exceeded 20 minutes")
            await wait_idle(probe.inspect)
            print(json.dumps({"event": "cold_boot_start", "kind": kind, "index": index + 1}), flush=True)
            started = time.monotonic()
            report = await probe.inspect.remote.aio()
            report.update(kind=kind, index=index + 1, clientSeconds=time.monotonic() - started)
            assert report["inspectionsInContainer"] == 1, "A warm container was reused"
            assert all(r["containerId"] != report["containerId"] for r in reports), "Container was reused"
            if schema_hash is None:
                schema_hash = report["schemaHash"]
            assert report["schemaHash"] == schema_hash, "ComfyUI schemas changed after restore"
            restored = kind == "SnapshotProbe" and report["initializationId"] in initialization_ids
            report["confirmedSnapshotReuse"] = restored
            if kind == "SnapshotProbe":
                initialization_ids.add(report["initializationId"])
                confirmed_restores += int(restored)
            reports.append(report)
            (output / "boots.json").write_text(json.dumps(reports, indent=2))
            print(json.dumps({key: report[key] for key in (
                "kind", "index", "clientSeconds", "comfyInitSeconds", "containerId",
                "initializationId", "confirmedSnapshotReuse", "nodeCount", "geminiNodeCount",
            )}), flush=True)
            await wait_idle(probe.inspect)
            print(json.dumps({"event": "scaled_to_zero", "kind": kind}), flush=True)
            if kind == "SnapshotProbe" and confirmed_restores >= 2:
                break
    if confirmed_restores < 2:
        raise RuntimeError("Not enough restored cold boots; inspect Modal snapshot logs")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-snapshot-boots", type=int, default=8)
    args = parser.parse_args()
    asyncio.run(compare(args.output, args.max_snapshot_boots))
