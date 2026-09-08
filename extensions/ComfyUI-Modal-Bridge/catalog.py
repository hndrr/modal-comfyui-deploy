"""Read-only ComfyUI metadata exposed through a standard extension route."""
import sys
from pathlib import Path
from aiohttp import web
from .cpu_guard import cpu_guard_enabled

async def catalog(_request):
    import nodes
    import folder_paths
    model_lists = {name: folder_paths.get_filename_list(name)
                   for name in folder_paths.folder_names_and_paths}
    choice_sources = {}
    for name, cls in nodes.NODE_CLASS_MAPPINGS.items():
        try:
            inputs = cls.INPUT_TYPES()
        except Exception:
            continue
        for section in ("required", "optional"):
            for field, definition in inputs.get(section, {}).items():
                if not definition or not isinstance(definition[0], list) or not definition[0]:
                    continue
                matches = [folder for folder, files in model_lists.items()
                           if files and definition[0] == files]
                if len(matches) == 1:
                    choice_sources.setdefault(name, {}).setdefault(section, {})[field] = matches[0]
    return web.json_response({
        "cpu_guard": cpu_guard_enabled(),
        "extensions": {name: str(path) for name, path in nodes.EXTENSION_WEB_DIRS.items()},
        "nodes": sorted(nodes.NODE_CLASS_MAPPINGS),
        "choice_sources": choice_sources,
        "import_failures": [str(p) for folder in folder_paths.get_folder_paths("custom_nodes")
                            for p in Path(folder).iterdir()
                            if getattr(sys, "__comfyui_manager_is_import_failed_extension", lambda _: False)(str(p))],
    })

