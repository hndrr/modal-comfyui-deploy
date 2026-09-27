"""Small gateway extension boundary. Importing split never imports an extension."""

from importlib import import_module
from typing import Protocol

from comfy_split.config import Settings


class Extension(Protocol):
    async def start(self): ...
    async def close(self): ...
    async def handle(self, request, path): ...
    def prepare(self, body): ...
    def event(self, event): ...


def load_extensions(controller):
    factories = {"ambient": ("ambient_comfyui.split", "SplitAmbient")}
    result = []
    for name in Settings.read().extensions:
        module, factory = factories[name]
        try:
            extension = getattr(import_module(module), factory)
        except ImportError as error:
            raise RuntimeError(f"Install the pinned {name} extension before enabling SPLIT_EXTENSIONS") from error
        result.append(extension(controller))
    return result
