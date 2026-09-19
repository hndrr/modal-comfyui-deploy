"""Versioned environments and supervised ComfyUI subprocesses."""

import asyncio
import configparser
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from aiohttp import ClientError, ClientSession

from comfy_split import ambient_nodes
from comfy_split.state import write_json
from comfy_split.storage import ENVIRONMENTS, TEMP_ARCHIVE, USER

TEMPLATE = Path("/opt/comfy-template")


def identifier(value):
    if not value or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in value):
        raise ValueError("Invalid environment identifier")
    return value


def environment_path(version):
    return ENVIRONMENTS / identifier(version)


def create_environment(source="base", *, restore_image_browsing=False):
    """Caller holds the maintenance lock. The active revision is never mutated."""
    version = "env-" + uuid.uuid4().hex
    target = environment_path(version)
    origin = environment_path(source)
    target.mkdir()
    shutil.copytree(origin / "comfy/custom_nodes", target / "comfy/custom_nodes", symlinks=True)
    shutil.copytree(origin / "venv", target / "venv", symlinks=True)
    if (origin / ambient_nodes.DIRECTORY).is_dir():
        shutil.copytree(origin / ambient_nodes.DIRECTORY, target / ambient_nodes.DIRECTORY,
                        symlinks=True)
    if (origin / ambient_nodes.MANIFEST).is_file():
        shutil.copyfile(origin / ambient_nodes.MANIFEST, target / ambient_nodes.MANIFEST)
    if restore_image_browsing:
        node = "ComfyUI-Image-Browsing"
        destination = target / "comfy/custom_nodes" / node
        if destination.is_symlink():
            destination.unlink()
        elif destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(TEMPLATE / "custom_nodes" / node, destination, symlinks=True)
    write_json(target / "ready.json", {"created_at": time.time(), "parent": source})
    # Virtualenv script shebangs are absolute. Keep the environment at this new
    # fixed path on both CPU and GPU instead of moving it again on activation.
    for entry in (target / "venv" / "bin").iterdir():
        if entry.is_symlink() or not entry.is_file():
            continue
        content = entry.read_bytes()
        if b"\x00" not in content:
            entry.write_bytes(content.replace(str(origin).encode(), str(target).encode()))
    for name in ("catalog.json", "validated.json"):
        (target / name).unlink(missing_ok=True)
    return version


def initialize_environment():
    user = USER
    user.mkdir(parents=True, exist_ok=True)
    if not (user / ".split-seeded.json").exists():
        seed = Path("/seed/user")
        if seed.exists():
            shutil.copytree(seed, user, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("*.db", "*.db-shm", "*.db-wal"))
        write_json(user / ".split-seeded.json", {"at": time.time()})
    target = environment_path("base")
    if (target / "ready.json").exists():
        return
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATE / "custom_nodes", target / "comfy/custom_nodes",
                    symlinks=True, dirs_exist_ok=True)
    # Existing user nodes are copied, not moved or updated in-place.
    existing = Path("/data/custom_nodes")
    if existing.exists():
        shutil.copytree(existing, target / "comfy" / "custom_nodes", dirs_exist_ok=True)
    subprocess.run([sys.executable, "-m", "venv", "--system-site-packages",
                    str(target / "venv")], check=True)
    python = str(target / "venv/bin/python")
    # Rehydrate dependencies of nodes copied from the old node Volume once.
    for node in sorted((target / "comfy/custom_nodes").iterdir()):
        requirements = node / "requirements.txt"
        if requirements.is_file():
            subprocess.run([python, "-m", "pip", "install", "-c", "/opt/split-constraints.txt",
                            "-r", str(requirements)], check=True)
    write_json(target / "ready.json", {"created_at": time.time()})


def configure_manager(user, enabled):
    path = user / "__manager" / "config.ini"
    path.parent.mkdir(parents=True, exist_ok=True)
    config = configparser.ConfigParser()
    if path.exists():
        config.read(path)
    if not config.has_section("default"):
        config.add_section("default")
    config["default"]["network_mode"] = "personal_cloud" if enabled else "public"
    config["default"]["security_level"] = "normal"
    # uv's package enumeration omits inherited system-site packages, causing
    # Manager to think the pinned Torch/frontend are missing in this venv.
    config["default"]["use_uv"] = "false"
    config["default"]["allow_git_url_install"] = str(enabled).lower()
    config["default"]["allow_pip_install"] = str(enabled).lower()
    with path.open("w") as handle:
        config.write(handle)


class ComfyProcess:
    def __init__(self, role, port):
        self.role, self.port = role, port
        self.process = None
        self.version = None
        self.dependencies = {}
        self.root = Path("/tmp") / ("split-comfy-" + role)
        self.log = Path("/tmp") / ("split-comfy-" + role + ".log")
        self.temp_root = self.root / "temporary"
        self.temp_namespace = uuid.uuid4().hex

    def archive_temp(self, *, legacy_paths=False):
        source = self.temp_root / "temp"
        if not source.exists():
            return
        destination = TEMP_ARCHIVE / ("temp" if legacy_paths else self.temp_namespace)
        shutil.copytree(source, destination, dirs_exist_ok=True)

    def durable_outputs(self, value):
        if isinstance(value, list):
            return [self.durable_outputs(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: self.durable_outputs(item) for key, item in value.items()}
        if result.get("type") == "temp" and isinstance(result.get("filename"), str):
            subfolder = Path(result.get("subfolder", ""))
            if subfolder.is_absolute() or ".." in subfolder.parts:
                raise ValueError("Invalid temporary output subfolder")
            result.update(type="output", subfolder=(Path(".split-temp") / self.temp_namespace / subfolder).as_posix())
        return result

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    async def stop(self):
        if self.process and self.process.returncode is None:
            await asyncio.to_thread(self.archive_temp, legacy_paths=self.role in {"cpu", "candidate"})
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 15)
            except asyncio.TimeoutError:
                os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
        self.process = None
        self.version = None
        self.dependencies = {}

    async def start(self, version, *, cpu=False, manager=False):
        if self.version == version and self.process and self.process.returncode is None:
            return
        await self.stop()
        source = environment_path(version)
        self.root.mkdir(parents=True, exist_ok=True)
        special = {"models", "input", "output", "user", "temp"}
        # Core code stays in the image: importing thousands of small files through
        # a Volume adds minutes to a cold start. Only mutable node packs live there.
        for template_path in TEMPLATE.iterdir():
            path = source / "comfy/custom_nodes" if template_path.name == "custom_nodes" else template_path
            if path.name in special:
                continue
            destination = self.root / path.name
            if destination.is_symlink():
                destination.unlink()
            elif destination.exists():
                continue
            destination.symlink_to(path, target_is_directory=path.is_dir())
        if self.role == "cpu":
            user = USER
        elif self.role == "candidate":
            user = source / "manager-user"
            if not user.exists():
                shutil.copytree(USER, user, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("*.db", "*.db-shm", "*.db-wal"))
        else:
            user = self.root / "user"
            shutil.rmtree(user, ignore_errors=True)
            shutil.copytree(USER, user, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("*.db", "*.db-shm", "*.db-wal"))
        user.mkdir(parents=True, exist_ok=True)
        configure_manager(user, manager)
        for name, destination in {
            "models": Path("/models"), "input": Path("/data/input"),
            "output": Path("/data/output"),
        }.items():
            destination.mkdir(parents=True, exist_ok=True)
            link = self.root / name
            if link.is_symlink():
                link.unlink()
            if not link.exists():
                link.symlink_to(destination, target_is_directory=True)
        self.temp_namespace = uuid.uuid4().hex
        temporary = self.temp_root
        temporary.mkdir(parents=True, exist_ok=True)
        extension_paths = Path(__file__).with_name("extension_paths.yaml")
        if ambient_nodes.enabled() and (source / ambient_nodes.DIRECTORY).is_dir():
            # JSON is valid YAML. Absolute Volume paths are identical on CPU/GPU.
            extension_paths = self.root / "ambient-extension-paths.json"
            write_json(extension_paths, {
                "modal_control": {"custom_nodes": "/opt/comfy-extensions"},
                "ambient": {"custom_nodes": str(source / ambient_nodes.DIRECTORY)},
            })
        command = [str(source / "venv/bin/python"), str(self.root / "main.py"),
                   "--listen", "127.0.0.1", "--port", str(self.port),
                   "--base-directory", str(self.root),
                   "--user-directory", str(user),
                   "--input-directory", "/data/input", "--output-directory", "/data/output",
                   "--temp-directory", str(temporary),
                   "--database-url", f"sqlite:////tmp/split-{self.role}.db",
                   "--enable-manager", "--preview-method", "auto",
                   "--extra-model-paths-config", str(extension_paths)]
        if cpu:
            command.append("--cpu")
        elif os.environ.get("COMFYUI_SAGE_ATTENTION", "on") == "on":
            command.append("--use-sage-attention")
        environment = dict(os.environ)
        environment.pop(ambient_nodes.TOKEN_ENV, None)
        environment.update(SPLIT_CPU="1" if cpu else "0", SPLIT_INTEGRATION="1",
                           PYTHONPATH="/opt/split:" + environment.get("PYTHONPATH", ""),
                           VIRTUAL_ENV=str(source / "venv"),
                           PATH=str(source / "venv/bin") + ":" + environment["PATH"])
        environment["PIP_CONSTRAINT"] = "/opt/split-constraints.txt"
        environment["COMFYUI_PATH"] = str(self.root)
        environment["COMFYUI_FOLDERS_BASE_PATH"] = str(self.root)
        with self.log.open("ab") as log:
            self.process = await asyncio.create_subprocess_exec(
                *command, cwd=self.root, env=environment, stdout=log,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        started = time.monotonic()
        async with ClientSession() as client:
            while time.monotonic() - started < 300:
                if self.process.returncode is not None:
                    break
                try:
                    async with client.get(self.url + "/object_info") as response:
                        if response.status == 200:
                            async with client.get(self.url + "/_split/catalog") as catalog_response:
                                if catalog_response.status != 200:
                                    raise RuntimeError("Modal bridge extension did not load")
                                metadata = await catalog_response.json()
                                if cpu and not metadata.get("cpu_guard"):
                                    raise RuntimeError("CPU execution guard did not load")
                            self.version = version
                            self.dependencies = metadata.get("dependencies", {})
                            print(json.dumps({"event": "comfy_ready", "role": self.role,
                                              "seconds": time.monotonic() - started}))
                            return
                except (OSError, ClientError, asyncio.TimeoutError):
                    pass
                except RuntimeError:
                    break
                await asyncio.sleep(1)
        tail = self.log.read_text(errors="replace")[-6000:]
        await self.stop()
        raise RuntimeError("ComfyUI startup failed: " + tail)

    async def catalog(self, client):
        async with client.get(self.url + "/object_info") as response:
            response.raise_for_status()
            objects = await response.json()
        async with client.get(self.url + "/_split/catalog") as response:
            response.raise_for_status()
            metadata = await response.json()
        return {"objects": objects, **metadata}
