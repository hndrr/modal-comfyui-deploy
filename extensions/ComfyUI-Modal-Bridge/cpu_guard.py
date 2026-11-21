"""Only remaining runtime override: reject direct custom-node CPU submissions.

ComfyUI has no supported execution-disabled mode. Blocking HTTP /prompt alone
cannot stop custom nodes calling PromptQueue.put. Do not alter worker startup,
queue reads, history, or cleanup. The normal prompt thread remains idle.
"""
import inspect


class CPUGenerationDisabled(RuntimeError):
    """The split gateway owns this CPU's generation queue."""


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
        raise CPUGenerationDisabled("Generation is owned by the split dispatcher")

    deny_put._modal_cpu_guard = True
    execution.PromptQueue.put = deny_put


def verify_cpu_guard():
    """Exercise the installed guard on the build CPU without starting a prompt worker."""
    from execution import PromptQueue

    install_cpu_guard()
    try:
        # The guard must reject before touching either queue state or prompt contents.
        PromptQueue.put(None, None)
    except CPUGenerationDisabled:
        return
    raise RuntimeError("The UI queue accepted a prompt with the CPU guard installed")
