"""Optional integrations; a plain split deployment needs none of them."""

from dataclasses import dataclass
import os

NODE_PACKS = {
    "agent-bridge": "hndrr/ComfyUI-AgentBridge",
    "agent-runtime": "hndrr/ComfyUI-AgentRuntime",
    "skills-loader": "hndrr/ComfyUI-Skills-Loader",
    "gemini": "hndrr/ComfyUI-GeminiTools",
    "jev": "hndrr/ComfyUI-Jev",
}
DEPLOYMENT_ENV = "SPLIT_DEPLOYMENT_ID"


def switch(environ, name, default="off"):
    value = environ.get(name, default).strip().lower()
    if value not in {"on", "off"}:
        raise ValueError(f"{name} must be on or off")
    return value == "on"


def selection(environ, name, allowed, default=""):
    values = tuple(dict.fromkeys(part.strip() for part in environ.get(name, default).split(",") if part.strip()))
    if set(values) - set(allowed):
        raise ValueError(f"{name} must select from: {', '.join(allowed)}")
    return values


@dataclass(frozen=True)
class Settings:
    extensions: tuple[str, ...] = ()
    node_packs: tuple[str, ...] = ()
    bridge: bool = False

    @classmethod
    def read(cls, environ=None):
        environ = os.environ if environ is None else environ
        # Each explicitly provided new setting wins, including an empty list.
        legacy = switch(environ, "COMFYUI_AMBIENT_MODE") if any(
            name not in environ for name in ("SPLIT_EXTENSIONS", "SPLIT_NODE_PACKS", "SPLIT_AGENT_BRIDGE")
        ) else False
        return cls(
            selection(environ, "SPLIT_EXTENSIONS", ("ambient",), "ambient" if legacy else ""),
            selection(environ, "SPLIT_NODE_PACKS", NODE_PACKS, ",".join(NODE_PACKS) if legacy else ""),
            switch(environ, "SPLIT_AGENT_BRIDGE", "on" if legacy else "off"),
        )

    def environment(self):
        return {"SPLIT_EXTENSIONS": ",".join(self.extensions),
                "SPLIT_NODE_PACKS": ",".join(self.node_packs),
                "SPLIT_AGENT_BRIDGE": "on" if self.bridge else "off"}

    def secrets(self, environ):
        """Only selected providers receive credentials; names, never values."""
        wanted = []
        if "gemini" in self.node_packs:
            wanted.append(("GEMINI_SECRET_NAME", "GEMINI_API_KEY"))
        if "jev" in self.node_packs:
            wanted.extend((("TYPESAFE_SECRET_NAME", "TYPESAFE_API_KEY"),
                           ("OPENROUTER_SECRET_NAME", "OPENROUTER_API_KEY")))
        if self.bridge:
            wanted.append(("AGENT_RUNTIME_SECRET_NAME", "AGENT_RUNTIME_BRIDGE_TOKEN"))
        return [(name, key, environ[name].strip()) for name, key in wanted if environ.get(name, "").strip()]
