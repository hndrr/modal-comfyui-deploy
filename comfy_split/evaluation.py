"""Names and resource bounds for disposable Servers comparisons."""

from dataclasses import dataclass
import re

from comfy_split.storage import VOLUME_NAMES

PREFIX = "comfyui-servers-eval-"


@dataclass(frozen=True)
class Evaluation:
    run_id: str
    backend: str

    def __post_init__(self):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,19}", self.run_id):
            raise ValueError("Evaluation ID must be 1–20 lowercase letters, digits or hyphens")
        if self.backend not in {"function", "server"}:
            raise ValueError("Evaluation backend must be function or server")

    @property
    def app_name(self):
        return PREFIX + self.run_id + "-" + self.backend

    @property
    def volumes(self):
        return {key: self.app_name + "-" + key.replace("_", "-") for key in VOLUME_NAMES}

    @classmethod
    def read(cls, env):
        return cls(env["SPLIT_EVALUATION_ID"], env["SPLIT_EVALUATION_BACKEND"])


def ui_handle(app_name, backend="function", name="ui"):
    """Both public SDK handles implement update_autoscaler with the same contract."""
    import modal

    if backend == "function":
        return modal.Function.from_name(app_name, name)
    if backend == "server":
        return modal.Server.from_name(app_name, name)
    raise ValueError("Unknown UI backend: " + backend)
