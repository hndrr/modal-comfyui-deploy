"""Only remaining runtime override: reject direct custom-node CPU submissions.

ComfyUI has no supported execution-disabled mode. Blocking HTTP /prompt alone
cannot stop custom nodes calling PromptQueue.put. Do not alter worker startup,
queue reads, history, or cleanup. The normal prompt thread remains idle.
"""
import inspect


def cpu_guard_enabled():
    import execution
    return getattr(execution.PromptQueue.put, "_modal_cpu_guard", False)


def install_cpu_guard():
    import execution
    if cpu_guard_enabled():
        return
    if list(inspect.signature(execution.PromptQueue.put).parameters) != ["self", "item"]:
        raise RuntimeError("Unsupported ComfyUI PromptQueue.put signature; CPU guard cannot be installed")

    def deny_put(self, item):
        raise RuntimeError("Generation is owned by the split GPU dispatcher")

    deny_put._modal_cpu_guard = True
    execution.PromptQueue.put = deny_put
