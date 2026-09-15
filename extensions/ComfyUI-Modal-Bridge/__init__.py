"""Optional Modal bridge, loaded by ComfyUI's standard custom-node loader."""
import os

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

if os.environ.get("SPLIT_INTEGRATION") == "1":
    from server import PromptServer
    from .catalog import catalog
    from .jobs import jobs
    from .cpu_guard import install_cpu_guard

    if os.environ.get("SPLIT_CPU") == "1":
        install_cpu_guard()
    PromptServer.instance.routes.get("/_split/catalog")(catalog)
    PromptServer.instance.routes.post("/_split/jobs")(jobs)
