"""Preload only ComfyUI; resume the gateway with fresh durable state afterwards.

The CPU image imports prepare() before Modal's memory snapshot point. The
existing ui() ASGI factory calls resume() after restoration, preserving its URL
and autoscaler identity. GPU images never run this module's preload hook.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid

from comfy_split import storage
from comfy_split.runtime import ComfyProcess, configure_manager, environment_path
from comfy_split.state import write_json

READY = Path("/tmp/split-snapshot-ready.json")
RESUME = Path("/tmp/split-snapshot-resume.json")
USER_LINK = Path("/tmp/split-snapshot-user")
USER_SEED = Path("/tmp/split-snapshot-user-seed")
_helper = None


def selected_environment():
    """Read only an immutable revision; never recover or save captured jobs."""
    path = storage.STATE / "controller.json"
    if not path.exists():
        return None
    state = json.loads(path.read_text())
    if state.get("mode") != "split":
        return None
    version = state["environment"]
    return version if (environment_path(version) / "ready.json").is_file() else None


def prepare():
    global _helper
    READY.unlink(missing_ok=True)
    RESUME.unlink(missing_ok=True)
    _helper = subprocess.Popen([sys.executable, "-m", "comfy_split.cpu_snapshot"],
                               env=dict(os.environ), start_new_session=True)
    started = time.monotonic()
    while time.monotonic() - started < 360:
        if _helper.poll() is not None:
            raise RuntimeError("CPU snapshot bootstrap exited before readiness")
        if READY.exists():
            info = json.loads(READY.read_text())
            if info.get("environment"):
                # The short-lived process owns its own Modal connection. No
                # authenticated client/session survives inside the warm helper.
                subprocess.run([sys.executable, "-c",
                    "import modal,os,json; modal.Volume.from_name("
                    "json.loads(os.environ['SPLIT_VOLUMES'])['environment']).commit()"],
                    check=True, timeout=60)
            print(json.dumps({"event": "cpu_snapshot_prepared", **info}), flush=True)
            return
        time.sleep(0.1)
    raise TimeoutError("CPU snapshot bootstrap exceeded 360 seconds")


def resume():
    """Called exclusively after Modal restores memory; returns the supervised PID."""
    if _helper is None or _helper.poll() is not None:
        raise RuntimeError("CPU snapshot bootstrap is not alive")
    # Modal refreshes credentials/environment only in its parent process.
    # The captured helper must receive those values before creating any client.
    temporary = RESUME.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"restoration_id": uuid.uuid4().hex,
                   "container_id": os.environ.get("MODAL_TASK_ID"),
                   "environment_variables": dict(os.environ)}, handle)
    temporary.replace(RESUME)
    return _helper


async def restore_controller(cpu, info, *, volumes, restoration):
    """Reload mounts before opening the journal or binding any gateway routes."""
    from aiohttp import ClientSession, ClientTimeout
    from comfy_split.gateway import make_controller

    for key, volume in volumes.items():
        # Loaded extension .so mappings keep the immutable environment mount
        # open. Its contents cannot change for this revision; reloading it is
        # necessary only after stopping that process for a different revision.
        if key == "environment" and cpu.version is not None:
            continue
        logging.info("Reloading snapshot mount: %s", key)
        await volume.reload.aio()
    if USER_LINK.is_symlink():
        USER_LINK.unlink()
    storage.USER.mkdir(parents=True, exist_ok=True)
    USER_LINK.symlink_to(storage.USER, target_is_directory=True)
    configure_manager(storage.USER, False)
    cpu.user_directory = None  # Restarts after an environment change use live user data.
    cpu.temp_namespace = uuid.uuid4().hex
    if cpu.version is not None:
        try:
            async with ClientSession(timeout=ClientTimeout(total=10)) as client:
                async with client.post(cpu.url + "/_split/restore") as response:
                    response.raise_for_status()
                    result = await response.json()
                    if not result.get("cpu_guard"):
                        raise RuntimeError("CPU guard missing after restore")
        except Exception:
            logging.exception("Warm ComfyUI health failed; starting a fresh CPU process")
            await cpu.stop()
    # Constructor reads today's journal, creates new locks, Bridge, workflow
    # registry and socket maps. Dispatcher starts later through StartupGate.
    controller = make_controller(volumes, warmed_cpu=cpu)
    if cpu.version is not None and (controller.journal.data["mode"] != "split" or
                                   controller.journal.data.get("candidate") or
                                   controller.journal.data["environment"] != cpu.version):
        await cpu.stop()
        await volumes["environment"].reload.aio()
    controller.snapshot_status = {**info, **restoration, "reused": False}
    return controller


async def bootstrap():
    from aiohttp import web

    logging.basicConfig(level=logging.INFO)
    version = selected_environment()
    cpu = ComfyProcess("cpu", 8187, user_directory=USER_LINK)
    started = time.monotonic()
    info = {"initialization_id": uuid.uuid4().hex, "environment": None}
    try:
        if version:
            # Keep Comfy/Manager's open user logs on local disk while taking the
            # snapshot so mounted mutable files can be reloaded on restoration.
            USER_SEED.mkdir(parents=True, exist_ok=True)
            if storage.USER.exists():
                shutil.copytree(storage.USER, USER_SEED, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("*.db*", "*.log*", "__pycache__"))
            USER_LINK.symlink_to(USER_SEED, target_is_directory=True)
            try:
                await cpu.start(version, cpu=True)
                info["environment"] = version
                deployment = os.environ["SPLIT_AMBIENT_DEPLOYMENT"]
                write_json(storage.ENVIRONMENTS / ".cpu-snapshots" /
                           f"{deployment}-{version}.json",
                           {"deployment": deployment, "environment": version, "created_at": time.time()})
            except Exception:
                # A broken optional warmup must retain the normal startup page
                # and environment recovery path after the snapshot point.
                logging.exception("CPU preload failed; falling back to normal startup")
                await cpu.stop()
        info["initialization_seconds"] = round(time.monotonic() - started, 3)
        write_json(READY, info)
        # No Controller, Queue, dispatcher or live HTTP client exists here.
        while not RESUME.exists():
            await asyncio.sleep(0.1)
        restoration = json.loads(RESUME.read_text())
        RESUME.unlink()
        os.environ.update(restoration.pop("environment_variables"))
        import modal
        from comfy_split.gateway import application
        volumes = {key: modal.Volume.from_name(name)
                   for key, name in json.loads(os.environ["SPLIT_VOLUMES"]).items()}
        controller = await restore_controller(cpu, info, volumes=volumes, restoration=restoration)
        runner = web.AppRunner(application(controller))
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", 8000).start()
        print(json.dumps({"event": "cpu_snapshot_resumed", **controller.snapshot_status}), flush=True)
        stopped = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, stopped.set)
        await stopped.wait()
        await runner.cleanup()
    finally:
        await cpu.stop()


if __name__ == "__main__":
    asyncio.run(bootstrap())
