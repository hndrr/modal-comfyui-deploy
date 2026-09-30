"""Optional extension pins. No package import, lookup or network access."""

AMBIENT = {
    "repository": "hndrr/ComfyUI-Ambient",
    "version": "0.1.0",
    "revision": "c40aa0d4cbde7ab570a04afe91590f03ba62d56f",
}

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
