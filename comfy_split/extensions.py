"""Optional integration hooks; importing split never imports an extension."""
from importlib import import_module
from typing import Protocol

from comfy_split.config import Settings
from comfy_split.proxy import proxy
from comfy_split.state import ACTIVE

# Declarative admission and route guards must work with the package uninstalled.
INTEGRATIONS = {
    "agent-bridge": {
        "module": "comfyui_agent_bridge.split", "factory": "SplitBridge",
        "prefix": "/agent_runtime/bridge/", "node_prefix": "AgentRuntimeBridge",
        "record_key": "agent_bridge", "setting": "SPLIT_AGENT_BRIDGE",
    },
}


class Extension(Protocol):
    async def start(self): ...
    async def close(self): ...
    async def handle(self, request, path): ...
    def prepare(self, body): ...
    def event(self, event): ...
    def admit(self, body, existing): ...
    def accepted(self, record, admission): ...
    def before_dispatch(self, record): ...
    async def worker_event(self, record, event): ...
    async def finished(self, record): ...
    def before_mode(self, desired): ...


class GatewayHost:
    """Capabilities exposed to an adapter, without exposing the Controller API."""
    proxy = staticmethod(proxy)
    active_statuses = frozenset(ACTIVE)

    def __init__(self, controller):
        self._controller = controller

    @property
    def client(self):
        return self._controller.client

    @property
    def cpu_url(self):
        return self._controller.cpu.url

    @property
    def jobs(self):
        return self._controller.journal.data["jobs"]

    @property
    def lock(self):
        return self._controller.lock

    @property
    def mode(self):
        return self._controller.journal.data["mode"]

    @property
    def candidate(self):
        return self._controller.journal.data["candidate"]


    async def command(self, job_id, kind):
        await self._controller.command(job_id, kind)


def enabled_integrations(settings=None):
    settings = settings or Settings.read()
    return ("agent-bridge",) if settings.bridge else ()


def reserved_route(path):
    return any(path.startswith(item["prefix"]) for item in INTEGRATIONS.values())


def check_requirements(body, record=None):
    enabled = enabled_integrations()
    nodes = body.get("prompt", {}) if isinstance(body, dict) else {}
    for name, item in INTEGRATIONS.items():
        required = isinstance(nodes, dict) and any(
            isinstance(node, dict) and str(node.get("class_type", "")).startswith(item["node_prefix"])
            for node in nodes.values())
        if name not in enabled and (required or (record or {}).get(item["record_key"])):
            raise ValueError(f"Enable {item['setting']} before submitting these nodes.")


def load_extensions(controller):
    result = []
    settings = Settings.read()
    factories = {"ambient": ("ambient_comfyui.split", "SplitAmbient")}
    for name in settings.extensions:
        module, factory = factories[name]
        try:
            extension = getattr(import_module(module), factory)
        except ImportError as error:
            raise RuntimeError(f"Install the pinned {name} extension before enabling SPLIT_EXTENSIONS") from error
        result.append(extension(controller))
    for name in enabled_integrations(settings):
        item = INTEGRATIONS[name]
        try:
            extension = getattr(import_module(item["module"]), item["factory"])
        except ImportError as error:
            raise RuntimeError(f"Install the pinned {name} package before enabling {item['setting']}") from error
        result.append(extension(GatewayHost(controller)))
    return result


async def run_worker_extensions(spec, host, generate):
    check_requirements(spec.get("body"), spec)
    run = generate
    for name in reversed(enabled_integrations()):
        wrapper = import_module(INTEGRATIONS[name]["module"]).run_generation
        inner = run
        async def wrapped(wrapper=wrapper, inner=inner):
            return await wrapper(spec, host, inner)
        run = wrapped
    return await run()
