"""Select a split worker at admission. The workflow itself stays portable."""

import asyncio
import os
from collections import defaultdict

DEFAULT = "RTX-PRO-6000"
WORKERS = {DEFAULT: "gpu_worker_rtx", "L4": "gpu_worker_l4", "L40S": "gpu_worker_l40s"}
CHOICES = ("auto", "auto-speed", "CPU", *WORKERS)

# CPU execution eligibility, independent of MCP's disclosure/approval policy.
# Add nodes here only after the CPU checks in docs/guides/split-gpu.md.
# Provider calls and result handling have not yet been verified on CPU.
CPU_API_NODES: dict[str, set[str]] = {}

# These nodes load or encode media without local model inference.
MEDIA_NODES = {
    "nodes": {"LoadImage", "SaveImage", "PreviewImage", "EmptyImage"},
    "comfy_extras.nodes_images": {"EmptyImage"},
    "comfy_extras.nodes_audio": {"LoadAudio", "SaveAudio", "SaveAudioMP3", "SaveAudioOpus", "PreviewAudio"},
    "comfy_extras.nodes_video": {"LoadVideo", "SaveVideo"},
}


def deployed_workers(app_name, session_gpu):
    import modal

    names = {**WORKERS, session_gpu: "gpu_worker"}
    if session_gpu in {"L4", "L40S"}:
        # The named pool can still own a container from the previous deployment.
        # Keep it visible even when new jobs route through the session worker.
        name = WORKERS[session_gpu]
        names[name] = name
    return {device: modal.Function.from_name(app_name, name) for device, name in names.items()}


async def pool_stats(workers):
    return await asyncio.wait_for(asyncio.gather(*(w.get_current_stats.aio() for w in workers)), timeout=3)


async def deployed_gpu_count():
    """Uncached control-plane count for the CPU-side acceptance scripts."""
    workers = deployed_workers(os.environ["SPLIT_APP"], os.getenv("SPLIT_SESSION_GPU", DEFAULT))
    return sum(s.num_total_runners for s in await pool_stats(workers.values()))


def request_gpu(value):
    if not isinstance(value, str) or value not in CHOICES:
        raise ValueError("gpu must be auto, auto-speed, CPU, L4, L40S or RTX-PRO-6000")
    return value


# GPU-only USD/second (Modal pricing). Compare first-generation elapsed time
# from ComfyUI startup through model loading, inference and PNG verification.
GPU_USD_PER_SECOND = {"L4": 0.000222, "L40S": 0.000542}
KLEIN_SECONDS = {
    512: {"L4": 50.16, "L40S": 23.17},
    1024: {"L4": 37.20, "L40S": 26.84},
}


def measured_still_gpu(graph, definitions):
    """Match the benchmark's model path and bounded work, independent of node IDs."""
    if os.getenv("COMFYUI_SAGE_ATTENTION", "on") != "on":
        return None
    nodes = defaultdict(list)
    for node in graph.values():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            return None
        kind = node.get("class_type")
        if not isinstance(kind, str):
            return None
        module = definitions.get(kind, {}).get("python_module")
        expected = "comfy_extras.nodes_flux" if kind == "EmptyFlux2LatentImage" else "nodes"
        if module != expected:
            return None
        nodes[kind].append(node["inputs"])
    inputs = {
        "UNETLoader": {"unet_name": "flux3-image-klein-int8-convrot.safetensors", "weight_dtype": "default"},
        "CLIPLoader": {"clip_name": "qwen3vl_4b_int8_convrot.safetensors", "type": "flux3"},
        "VAELoader": {"vae_name": "flux2-vae.safetensors"},
        "EmptyFlux2LatentImage": {"batch_size": 1},
        "KSampler": {"steps": 4, "cfg": 1, "denoise": 1, "sampler_name": "euler", "scheduler": "simple"},
        "VAEDecode": {},
    }
    if nodes.keys() - inputs.keys() - {"CLIPTextEncode", "SaveImage", "PreviewImage"}:
        return None
    for kind, expected in inputs.items():
        if len(nodes[kind]) != 1 or any(nodes[kind][0].get(k) != v for k, v in expected.items()):
            return None
    if (not 1 <= len(nodes["CLIPTextEncode"]) <= 2 or len(nodes["SaveImage"]) + len(nodes["PreviewImage"]) != 1
            or nodes["CLIPLoader"][0].get("device", "default") != "default"):
        return None
    latent = nodes["EmptyFlux2LatentImage"][0]
    if latent.get("width") != latent.get("height"):
        return None
    # Linked/dynamic inputs or very long prompts are outside the measured profile.
    if not all(isinstance(n.get("text"), str) and len(n["text"].encode("utf-8")) <= 256
               for n in nodes["CLIPTextEncode"]):
        return None
    size = latent.get("width")
    if type(size) is not int or size not in KLEIN_SECONDS:
        return None
    timings = KLEIN_SECONDS[size]
    return min(timings, key=lambda device: timings[device] * GPU_USD_PER_SECOND[device])


def select_gpu(requested, graph, definitions):
    """Route verified, currently CPU-importable graphs before applying GPU preferences."""
    if requested == "CPU":
        return {"gpu": "CPU", "gpu_reason": "明示的なCPU指定"}
    speed = requested == "auto-speed"
    priority = "速度優先" if speed else "料金優先"
    unsupported, unavailable, cpu_missing = set(), set(), set()
    has_provider = False
    for node in graph.values():
        if not isinstance(node, dict) or not isinstance(node.get("class_type"), str) or not isinstance(node.get("inputs"), dict):
            unavailable.add("不正なノード入力")
            continue
        name = node["class_type"]
        info = definitions.get(name, {})
        module = info.get("python_module", "").lower()
        if not module:
            unavailable.add(name)
            continue
        if info.get("_split_cpu_imported") is False:
            cpu_missing.add(name)
            continue
        if name in MEDIA_NODES.get(module, ()):
            if name == "EmptyImage":
                inputs = node["inputs"]
                dims = [inputs.get(k) for k in ("width", "height", "batch_size")]
                if not all(type(n) is int and n > 0 for n in dims) or dims[0] * dims[1] * dims[2] > 4096**2:
                    unsupported.add(name)
            continue
        if name in CPU_API_NODES.get(module, ()) and (
            not module.startswith("comfy_api_nodes.") or info.get("api_node") is True
        ):
            has_provider = True
        else:
            unsupported.add(name)
    if graph and not (unsupported or unavailable or cpu_missing):
        reason = "外部APIと素材の入出力のみ" if has_provider else "素材の入出力のみ"
        return {"gpu": "CPU", "gpu_reason": f"{reason}：CPUで実行（GPU指定より優先）"}
    reasons = []
    for label, names in (("CPU判定に必要なノード定義・入力を確認できません", unavailable),
                         ("CPUで読み込めないノード", cpu_missing),
                         ("CPU対応未登録または対象条件外", unsupported)):
        if names:
            shown = ", ".join(name[:80] for name in sorted(names)[:5])
            rest = f"、ほか{len(names) - 5}件" if len(names) > 5 else ""
            reasons.append(f"{label}：{shown}{rest}")
    detail = "。".join(reasons) or "実行対象のノードを確認できません"
    if requested in WORKERS:
        return {"gpu": requested, "gpu_reason": "ジョブの機種指定。" + detail}
    # Preserve the existing GPU choice while CPU verification is pending.
    # MCP classification selects a GPU tier here; it never grants CPU eligibility.
    from comfy_split.mcp_policy import classification

    if graph and not unavailable:
        for node in graph.values():
            name = node["class_type"]
            info = definitions[name]
            module = info["python_module"].lower()
            if name in MEDIA_NODES.get(module, ()) or (
                module == "custom_nodes.comfyui-geminitools" and name == "GeminiToolsSaveAudio"
            ):
                if name == "EmptyImage":
                    dims = [node["inputs"].get(k) for k in ("width", "height", "batch_size")]
                    if not all(type(n) is int and n > 0 for n in dims) or dims[0] * dims[1] * dims[2] > 4096**2:
                        break
                continue
            try:
                if not classification(name, info, node["inputs"]):
                    break
            except ValueError:
                break
        else:
            return {"gpu": "L4", "gpu_reason": f"{priority}：外部API・軽い素材入出力の従来GPUを選択。{detail}"}
    if not speed:
        measured = measured_still_gpu(graph, definitions)
        if measured is not None:
            return {"gpu": measured, "gpu_reason": "料金優先：FLUX.3 Kleinの実測時間とGPU単価から選択。" + detail}
    return {"gpu": DEFAULT, "gpu_reason": f"{priority}：ローカル処理・未検証の処理に大容量GPUを選択。{detail}"}
