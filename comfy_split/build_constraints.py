"""Write pip constraints before installing upstream or custom-node requirements."""

from importlib.metadata import version
from pathlib import Path
import sys


ACCELERATOR_PACKAGES = ("torch", "torchvision", "torchaudio", "xformers", "flash-attn", "sageattention")


def core_pins(requirements):
    for name in ("comfy-kitchen", "comfyui-frontend-package"):
        pin = next((line.strip() for line in requirements.splitlines()
                    if line.strip().startswith(name + "==")), None)
        if pin is None:
            raise RuntimeError(f"Upstream ComfyUI must pin {name}; review its requirements")
        yield pin


def build_constraints(requirements, manager, installed=version):
    return [*(f"{name}=={installed(name)}" for name in ACCELERATOR_PACKAGES),
            *core_pins(requirements), f"comfyui-manager=={manager}"]


if __name__ == "__main__":
    print(*build_constraints(Path(sys.argv[1]).read_text(), sys.argv[2]), sep="\n")
