"""Optional extension pins. No package import, lookup or network access."""

from pathlib import Path
import tomllib

_CATALOG = tomllib.loads(Path(__file__).with_name("extension_catalog.toml").read_text())
EXTENSIONS = _CATALOG["extensions"]
LEGACY = _CATALOG["legacy"]

BRIDGE = {
    "repository": "hndrr/ComfyUI-AgentBridge",
    "version": "0.2.0",
    "revision": "0dd4ac5a8c9c8550d13b3ae3f4f536c50336106b",
}

# Coordinated extraction: never combine new Bridge with AgentRuntime's old nodes.
AGENT_RUNTIME = {
    "repository": "hndrr/ComfyUI-AgentRuntime",
    "revision": "b1ed92f74b0330eee2939642519d97454a45a350",
}


def node_revision(repository):
    return next((source["revision"] for source in (BRIDGE, AGENT_RUNTIME)
                 if repository == source["repository"]), None)
