"""Optional integration hooks; importing split never imports an extension."""
from importlib import import_module
from typing import Protocol

from comfy_split.config import Settings
from comfy_split.extension_sources import INTEGRATIONS, enabled_sources
from comfy_split.proxy import proxy
from comfy_split.state import ACTIVE

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
    return settings.integrations


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
    for name, source in enabled_sources(Settings.read()):
        try:
            extension = getattr(import_module(source["module"]), source["factory"])
        except ImportError as error:
            setting = source.get("setting", "SPLIT_EXTENSIONS")
            raise RuntimeError(f"Install the pinned {name} package before enabling {setting}") from error
        host = GatewayHost(controller) if source.get("host") == "capabilities" else controller
        result.append(extension(host))
    return result


async def run_worker_extensions(spec, host, generate):
    check_requirements(spec.get("body"), spec)
    run = generate
    for name in reversed(enabled_integrations()):
        source = INTEGRATIONS[name]
        if not source.get("worker"):
            continue
        wrapper = getattr(import_module(source["module"]), source["worker"])
        inner = run
        async def wrapped(wrapper=wrapper, inner=inner):
            return await wrapper(spec, host, inner)
        run = wrapped
    return await run()
