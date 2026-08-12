import filecmp
import importlib
import os
import shlex
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Final

MODEL_VOLUME_DIR: Final = Path("/models")
CUSTOM_NODE_VOLUME_MOUNT: Final = Path("/data/custom_nodes")
OUTPUT_VOLUME_MOUNT: Final = Path("/data/output")
INPUT_VOLUME_MOUNT: Final = Path("/data/input")
USER_DATA_VOLUME_MOUNT: Final = Path("/data/user")
COMFY_ROOT_CANDIDATES: Final = (
    Path("/root/comfy/ComfyUI"),
    Path("/root/ComfyUI"),
    Path("/root/.cache/comfyui/ComfyUI"),
)
COMFYUI_CLI_ARGS_ENV: Final = "COMFYUI_CLI_ARGS"
COMFYUI_EXPECTED_CUDA_ARCH_ENV: Final = "COMFYUI_EXPECTED_CUDA_ARCH"
COMFYUI_REQUIRED_KITCHEN_CAPABILITIES_ENV: Final = (
    "COMFYUI_REQUIRED_KITCHEN_CAPABILITIES"
)
COMFYUI_SAGE_ATTENTION_ENV: Final = "COMFYUI_SAGE_ATTENTION"
EXPECTED_COMFY_KITCHEN_VERSION: Final = "0.2.30"
EXPECTED_TORCH_CUDA_MAJOR: Final = 13
MINIMUM_NVIDIA_DRIVER_MAJOR: Final = 580
SAGE_ATTENTION_FLAG: Final = "--use-sage-attention"
WORKFLOWS_PATCH_MARKER: Final = "# COMFYUI_PATCH_ALLOW_WORKFLOWS_START"
WORKFLOWS_PATCH_SNIPPET: Final = """
# COMFYUI_PATCH_ALLOW_WORKFLOWS_START
def _comfyui_allow_workflows():
    _candidates = (
        "ALLOWED_JSON_TYPES",
        "ALLOWED_TYPES",
        "ALLOWED_JSON_DIRS",
        "ALLOWED_DIRS",
    )
    for _name in _candidates:
        _container = globals().get(_name)
        if isinstance(_container, list):
            if "workflows" not in _container:
                _container.append("workflows")
        elif isinstance(_container, set):
            if "workflows" not in _container:
                _container.add("workflows")
        elif isinstance(_container, tuple):
            if "workflows" not in _container:
                globals()[_name] = _container + ("workflows",)

    for _name in ("ALLOWED_JSON_TYPES_MAP", "ALLOWED_TYPES_MAP"):
        _mapping = globals().get(_name)
        if isinstance(_mapping, dict) and "workflows" not in _mapping:
            _mapping["workflows"] = "json"

_comfyui_allow_workflows()
del _comfyui_allow_workflows
# COMFYUI_PATCH_ALLOW_WORKFLOWS_END
""".lstrip("\n")


def resolve_switch(value: str, *, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "on":
        return True
    if normalized == "off":
        return False
    raise ValueError(f"Invalid {name}: {value!r}. Allowed values: on, off")


def build_launch_command(extra_cli_args: str, *, sage_attention: bool) -> list[str]:
    command = [
        "comfy",
        "launch",
        "--",
        "--listen",
        "0.0.0.0",
        "--port",
        "8000",
        "--preview-method",
        "auto",
    ]
    cli_args = shlex.split(extra_cli_args) if extra_cli_args else []
    if sage_attention and SAGE_ATTENTION_FLAG not in cli_args:
        command.append(SAGE_ATTENTION_FLAG)
    command.extend(cli_args)
    return command


def _query_nvidia_smi() -> tuple[str, int]:
    try:
        output = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version",
                "--format=csv,noheader",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "nvidia-smi failed; CUDA 13 driver compatibility is unknown"
        ) from exc

    first_line = output.splitlines()[0] if output else ""
    try:
        driver_major = int(first_line.rsplit(",", 1)[1].strip().split(".", 1)[0])
    except (IndexError, ValueError) as exc:
        raise RuntimeError(f"Could not parse nvidia-smi output: {output!r}") from exc
    if driver_major < MINIMUM_NVIDIA_DRIVER_MAJOR:
        raise RuntimeError(
            "CUDA 13 requires NVIDIA driver 580 or newer; "
            f"nvidia-smi reported {first_line}"
        )
    return output, driver_major


def _load_comfyui_quantization_policy(comfy_roots: tuple[Path, ...]) -> None:
    for root in reversed(comfy_roots):
        root_string = str(root)
        if root_string not in sys.path:
            sys.path.insert(0, root_string)
    importlib.import_module("comfy.quant_ops")


def validate_acceleration(
    expected_arch: str,
    *,
    required_capabilities: str = "",
    comfy_roots: tuple[Path, ...] = (),
) -> dict[str, str]:
    """Fail before launch if Beam attached the wrong GPU or Kitchen lacks CUDA."""

    if not expected_arch:
        raise RuntimeError(f"{COMFYUI_EXPECTED_CUDA_ARCH_ENV} is required")

    nvidia_smi, driver_major = _query_nvidia_smi()

    import comfy_kitchen
    import torch

    try:
        kitchen_version = package_version("comfy-kitchen")
    except PackageNotFoundError as exc:
        raise RuntimeError("Comfy Kitchen package metadata was not found") from exc
    if kitchen_version != EXPECTED_COMFY_KITCHEN_VERSION:
        raise RuntimeError(
            "Unexpected Comfy Kitchen version: "
            f"expected {EXPECTED_COMFY_KITCHEN_VERSION}, got {kitchen_version}"
        )

    if not torch.cuda.is_available():
        raise RuntimeError("PyTorch cannot access a CUDA GPU")

    torch_cuda = str(torch.version.cuda)
    try:
        torch_cuda_major = int(torch_cuda.split(".", 1)[0])
    except ValueError as exc:
        raise RuntimeError(
            f"Could not parse PyTorch CUDA version: {torch_cuda!r}"
        ) from exc
    if torch_cuda_major != EXPECTED_TORCH_CUDA_MAJOR:
        raise RuntimeError(
            f"PyTorch CUDA {EXPECTED_TORCH_CUDA_MAJOR}.x is required; got {torch_cuda}"
        )

    device = torch.cuda.current_device()
    capability = ".".join(
        str(part) for part in torch.cuda.get_device_capability(device)
    )
    if capability != expected_arch:
        raise RuntimeError(
            f"Unexpected CUDA compute capability: expected {expected_arch}, got {capability}"
        )

    if comfy_roots:
        _load_comfyui_quantization_policy(comfy_roots)

    backends = comfy_kitchen.list_backends()
    cuda_backend = backends.get("cuda", {})
    if not cuda_backend.get("available") or cuda_backend.get("disabled"):
        reason = cuda_backend.get("unavailable_reason") or "unknown reason"
        raise RuntimeError(f"Comfy Kitchen CUDA backend is unavailable: {reason}")

    available_capabilities = set(cuda_backend.get("capabilities", []))
    required = {
        item.strip() for item in required_capabilities.split(",") if item.strip()
    }
    missing = sorted(required - available_capabilities)
    if missing:
        raise RuntimeError(
            "Comfy Kitchen CUDA backend is missing required capabilities: "
            + ", ".join(missing)
        )

    return {
        "device": torch.cuda.get_device_name(device),
        "compute_capability": capability,
        "torch": str(torch.__version__),
        "torch_cuda": torch_cuda,
        "comfy_kitchen": kitchen_version,
        "kitchen_cuda_capabilities": ",".join(cuda_backend.get("capabilities", [])),
        "nvidia_smi": nvidia_smi,
        "driver_major": str(driver_major),
    }


def _merge_directory_contents(source_dir: Path, target_dir: Path) -> None:
    """Move an image-provided directory into its persistent volume."""

    for item in list(target_dir.iterdir()):
        destination = source_dir / item.name

        if item.is_dir():
            if destination.exists():
                if destination.is_dir():
                    shutil.copytree(item, destination, dirs_exist_ok=True)
                    shutil.rmtree(item)
                else:
                    shutil.move(str(item), destination.with_suffix(".dir_conflict"))
            else:
                shutil.move(str(item), destination)
            continue

        if destination.exists():
            try:
                same_file = destination.is_file() and filecmp.cmp(
                    item, destination, shallow=False
                )
            except OSError:
                same_file = False
            if same_file:
                item.unlink()
            else:
                shutil.move(str(item), destination.with_suffix(".conflict"))
        else:
            shutil.move(str(item), destination)


def link_directory(target: Path, source: Path) -> bool:
    """Replace a ComfyUI directory with a symlink to persistent storage."""

    source.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.is_symlink():
        if Path(os.readlink(target)) != source:
            target.unlink()
            target.symlink_to(source, target_is_directory=True)
        return True

    if target.exists():
        if not target.is_dir():
            print(f"Warning: {target} is a file; persistent storage was not linked")
            return False

        _merge_directory_contents(source, target)
        if any(target.iterdir()):
            print(f"Warning: {target} could not be emptied; it was not linked")
            return False
        target.rmdir()

    target.symlink_to(source, target_is_directory=True)
    return True


def patch_user_manager_for_workflows(comfy_root: Path) -> None:
    """Allow workflow JSON paths in ComfyUI's user-data API."""

    candidate_paths = (
        comfy_root / "comfy" / "ui" / "user_manager.py",
        comfy_root / "app" / "user_manager.py",
    )
    replacements = {
        '@routes.get("/userdata/{file}")': '@routes.get(r"/userdata/{file:.*}")',
        "@routes.get('/userdata/{file}')": "@routes.get(r'/userdata/{file:.*}')",
        '@routes.post("/userdata/{file}")': '@routes.post(r"/userdata/{file:.*}")',
        "@routes.post('/userdata/{file}')": "@routes.post(r'/userdata/{file:.*}')",
        '@routes.delete("/userdata/{file}")': '@routes.delete(r"/userdata/{file:.*}")',
        "@routes.delete('/userdata/{file}')": "@routes.delete(r'/userdata/{file:.*}')",
        '@routes.post("/userdata/{file}/move/{dest}")': (
            '@routes.post(r"/userdata/{file:.*}/move/{dest:.*}")'
        ),
        "@routes.post('/userdata/{file}/move/{dest}')": (
            "@routes.post(r'/userdata/{file:.*}/move/{dest:.*}')"
        ),
    }

    for user_manager_path in candidate_paths:
        if not user_manager_path.exists():
            continue
        try:
            updated = user_manager_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Warning: could not read {user_manager_path}: {exc}")
            continue

        original = updated
        for old, new in replacements.items():
            if new not in updated and old in updated:
                updated = updated.replace(old, new)
        if WORKFLOWS_PATCH_MARKER not in updated:
            updated = f"{updated.rstrip()}\n\n{WORKFLOWS_PATCH_SNIPPET}"

        if updated == original:
            continue
        try:
            user_manager_path.write_text(updated, encoding="utf-8")
            print(f"Patched workflow persistence in {user_manager_path}")
        except OSError as exc:
            print(f"Warning: could not patch {user_manager_path}: {exc}")


def prepare_persistent_storage(
    *,
    comfy_roots: tuple[Path, ...] = COMFY_ROOT_CANDIDATES,
    models: Path = MODEL_VOLUME_DIR,
    custom_nodes: Path = CUSTOM_NODE_VOLUME_MOUNT,
    output: Path = OUTPUT_VOLUME_MOUNT,
    input_dir: Path = INPUT_VOLUME_MOUNT,
    user: Path = USER_DATA_VOLUME_MOUNT,
) -> list[Path]:
    volume_paths = (models, custom_nodes, output, input_dir, user)
    for path in volume_paths:
        path.mkdir(parents=True, exist_ok=True)

    existing_roots = [root for root in comfy_roots if root.exists()]
    if not existing_roots:
        expected = ", ".join(str(root) for root in comfy_roots)
        raise FileNotFoundError(
            f"ComfyUI installation was not found. Checked: {expected}"
        )

    links = {
        "models": models,
        "custom_nodes": custom_nodes,
        "output": output,
        "input": input_dir,
        "user": user,
    }
    for comfy_root in existing_roots:
        patch_user_manager_for_workflows(comfy_root)
        for relative_path, source in links.items():
            target = comfy_root / relative_path
            if link_directory(target, source):
                print(f"Linked {target} -> {source}")

    return existing_roots


def main() -> None:
    roots = prepare_persistent_storage()
    acceleration = validate_acceleration(
        os.environ.get(COMFYUI_EXPECTED_CUDA_ARCH_ENV, "").strip(),
        required_capabilities=os.environ.get(
            COMFYUI_REQUIRED_KITCHEN_CAPABILITIES_ENV, ""
        ),
        comfy_roots=tuple(roots),
    )
    print(
        "Acceleration: "
        + "; ".join(f"{key}={value}" for key, value in acceleration.items()),
        flush=True,
    )
    extra_cli_args = os.environ.get(COMFYUI_CLI_ARGS_ENV, "").strip()
    sage_attention = resolve_switch(
        os.environ.get(COMFYUI_SAGE_ATTENTION_ENV, "off"),
        name=COMFYUI_SAGE_ATTENTION_ENV,
    )
    command = build_launch_command(
        extra_cli_args,
        sage_attention=sage_attention,
    )
    print(f"ComfyUI roots: {', '.join(str(root) for root in roots)}")
    print(f"Launching: {shlex.join(command)}", flush=True)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
