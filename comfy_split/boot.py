"""Run the pinned ComfyUI without allowing the CPU process to execute prompts."""

import os
import sys
from pathlib import Path

# This file is run as a script with the runtime ComfyUI directory as cwd.
sys.path.insert(0, os.getcwd())
import main  # noqa: E402

if os.environ.get("SPLIT_CPU") == "1":
    main.prompt_worker = lambda *_: None
    # The API gateway owns execution. This also stops custom routes accidentally
    # submitting work to the local CPU queue.
    import execution

    def deny_put(*_args, **_kwargs):
        raise RuntimeError("Generation is owned by the split GPU dispatcher")

    execution.PromptQueue.put = deny_put

    def state():
        from comfy_split.state import Journal
        return Journal(Path("/state"))

    execution.PromptQueue.get_current_queue = lambda self: (
        state().queue()["queue_running"], state().queue()["queue_pending"])
    execution.PromptQueue.get_current_queue_volatile = execution.PromptQueue.get_current_queue
    execution.PromptQueue.get_tasks_remaining = lambda self: sum(
        len(items) for items in state().queue().values())

    def history(self, prompt_id=None, max_items=None, offset=-1):
        result = state().history()
        if prompt_id:
            return {prompt_id: result[prompt_id]} if prompt_id in result else {}
        items = list(result.items())
        if offset >= 0:
            items = items[offset:]
        if max_items is not None:
            items = items[-max_items:]
        return dict(items)

    execution.PromptQueue.get_history = history

# Avoid deleting persisted temporary outputs at every ComfyUI start/stop.
main.cleanup_temp = lambda: None
loop, server, start = main.start_comfyui()

# Extract web directories as well as node definitions. GPU-only packs can expose
# frontend extensions even when their Python module cannot be imported on CPU.
import nodes  # noqa: E402
from aiohttp import web  # noqa: E402


async def catalog(_request):
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
        "extensions": {name: str(path) for name, path in nodes.EXTENSION_WEB_DIRS.items()},
        "nodes": sorted(nodes.NODE_CLASS_MAPPINGS),
        "choice_sources": choice_sources,
        "import_failures": [str(p) for folder in folder_paths.get_folder_paths("custom_nodes")
                            for p in Path(folder).iterdir()
                            if getattr(sys, "__comfyui_manager_is_import_failed_extension", lambda _: False)(str(p))],
    })


server.app.router.add_get("/_split/catalog", catalog)
loop.run_until_complete(start())
