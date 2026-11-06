"""Admission policy for machine-submitted ComfyUI API graphs. No provider calls."""

import hashlib
import os
import re
from copy import deepcopy
from pathlib import Path

from comfy_split import input_media
from comfy_split.config import DEPLOYMENT_ENV
from comfy_split.production import parts, regular
from comfy_split.state import body_digest
from comfy_split.storage import MOUNTS

# Trust installed class provenance, not names supplied by the workflow. New packs
# require an explicit review; merely appearing in object_info is not authorization.
LOCAL_MODULES = {"nodes"} | {
    "comfy_extras.nodes_" + name
    for name in [
        "advanced_samplers",
        "audio",
        "chroma_radiance",
        "clip_sdxl",
        "compositing",
        "controlnet",
        "cosmos",
        "custom_sampler",
        "differential_diffusion",
        "flux",
        "hunyuan",
        "hypernetwork",
        "image_compare",
        "images",
        "latent",
        "mask",
        "minimax_h3",
        "minimax_music",
        "model_advanced",
        "model_merging",
        "model_merging_model_specific",
        "model_patch",
        "photomaker",
        "post_processing",
        "qwen",
        "sd3",
        "stable_cascade",
        "upscale_model",
        "video",
        "wan",
    ]
}
LOCAL_PACKS = {
    "custom_nodes.comfyui-videohelpersuite": {"VHS_VideoCombine"},
    "custom_nodes.comfyui-minimax-h3-motioncache-fastvae": {
        "MiniMaxH3MotionCache",
        "MiniMaxH3FastVAEDecode",
    },
    "custom_nodes.comfyui-kjnodes": {
        "MiniMaxH3MemoryEfficientSageAttentionPatch",
    },
    "custom_nodes.comfyui-geminitools": {
        "GeminiToolsBBoxOverlay",
        "GeminiToolsImageRegionCrop",
        "GeminiToolsImageRegionComposite",
        "GeminiToolsTrackBatchPack",
        "GeminiToolsTrackBatchUnpack",
        "GeminiToolsSaveAudio",
    },
}
DIRECT_GEMINI = {"GeminiToolsLLM", "GeminiToolsImageGeneration", "GeminiToolsMusicGeneration"}
PARTNERS = {
    "openai": {"OpenAIChatNode", "OpenAIGPTImage1", "OpenAIGPTImageNodeV2"},
    "openrouter": {"OpenRouterLLMNode", "OpenRouterImageNode"},
    "gemini": {
        "GeminiNode",
        "GeminiNodeV2",
        "GeminiNodeV3",
        "GeminiImageNode",
        "GeminiImage2Node",
        "GeminiNanoBanana2",
        "GeminiNanoBanana2V2",
    },
    "sora": {"OpenAIVideoSora2"},
    "comfy_cloud": {
        "ComfyCloudMiniMaxMusic3TextToAudioNode",
        "ComfyCloudMiniMaxH3TextToVideoNode",
        "ComfyCloudMiniMaxH3FirstLastFrameToVideoNode",
        "ComfyCloudMiniMaxH3ImageToVideoNode",
        "ComfyCloudFlux2TextToImageNode",
        "ComfyCloudZImageTurboNode",
        "ComfyCloudMageFlowTextToImageNode",
        "ComfyCloudMageFlowTurboTextToImageNode",
    },
}
MEDIA_LOADERS = {"LoadImage": "image", "LoadAudio": "audio", "LoadVideo": "file"}
MEDIA_EXT = input_media.EXTENSIONS
MAX_MEDIA = input_media.MAX
COMFY = "Comfy アカウントのクレジット（サーバー設定の Comfy API キー）"


def billing_account(billing):
    label_var = (
        "SPLIT_COMFY_BILLING_LABEL"
        if billing == COMFY
        else "SPLIT_GEMINI_BILLING_LABEL"
        if "Google Gemini" in billing
        else "SPLIT_OPENROUTER_BILLING_LABEL"
    )
    return os.environ.get(label_var, "").strip() or None


def classification(name, definition, inputs):
    module = definition.get("python_module", "").lower()
    if module.startswith("comfy_api_nodes.") and definition.get("api_node") is True:
        family = module.rsplit(".", 1)[-1].removeprefix("nodes_")
        if name not in PARTNERS.get(family, ()):
            raise ValueError(f"{name}: 外部への送信内容を確認できないノードです。")
        provider = {
            "openai": "OpenAI",
            "sora": "OpenAI",
            "gemini": "Google Gemini",
            "openrouter": "OpenRouter",
            "comfy_cloud": "Comfy Cloud",
        }.get(family, family)
        return provider, COMFY
    if module == "custom_nodes.comfyui-geminitools" and name in DIRECT_GEMINI:
        return "Google Gemini", "Google Gemini API（サーバー設定の Google API キー）"
    if module == "custom_nodes.comfyui-jev" and name == "OpenRouterText":
        return "OpenRouter", "OpenRouter アカウント（サーバー設定の OpenRouter API キー）"
    if not definition.get("api_node") and (
        module in LOCAL_MODULES or name in LOCAL_PACKS.get(module, ())
    ):
        return None
    raise ValueError(f"{name}: 実行先を確認できないノードです。MCP からは実行できません。")


def reject_secrets(inputs):
    for key, value in inputs.items():
        if value and re.search(
            r"(api[_.-]?key|authorization|credential|password|secret|auth_token|access_token|base_url|endpoint)",
            key,
            re.IGNORECASE,
        ):
            raise ValueError(
                "認証情報や接続先をグラフで指定できません。サーバー側の設定を使ってください。"
            )


def is_link(value):
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and type(value[1]) is int
    )


def executable_graph(graph, definitions, targets=None):
    """Remove disconnected branches from the payload itself, before classifying it.

    Every registered output is a root (including output nodes with side effects).
    If older catalog entries lack output metadata, keep the entire graph and
    inspect it conservatively. Never trust output flags supplied by the caller.
    """
    if not isinstance(graph, dict) or not graph or len(graph) > 4096:
        raise ValueError("Expected a bounded API workflow.")
    roots = []
    for node_id, node in graph.items():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            raise TypeError("Expected API nodes with explicit inputs.")
        output = definitions.get(node.get("class_type"), {}).get("output_node")
        if type(output) is not bool:
            return graph
        if output and (targets is None or node_id in targets):
            roots.append(node_id)
    if not roots:
        raise ValueError("Workflow has no executable output. Connect a save or preview node.")
    needed = set()
    while roots:
        node_id = roots.pop()
        if node_id in needed:
            continue
        if node_id not in graph:
            raise ValueError("Workflow refers to a missing node.")
        needed.add(node_id)
        roots.extend(v[0] for v in graph[node_id]["inputs"].values() if is_link(v))
    return {k: v for k, v in graph.items() if k in needed}


def plan(graph, definitions):
    """Return the specific calls and exactly which source inputs must be frozen."""
    calls, loaders = [], {}
    if not isinstance(graph, dict) or not graph or len(graph) > 4096:
        raise ValueError("Expected a bounded API workflow.")
    for node_id, node in graph.items():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            raise TypeError("Expected API nodes with explicit inputs.")
        name, inputs = node.get("class_type"), node["inputs"]
        if not isinstance(name, str) or name not in definitions:
            raise ValueError(f"Unknown node: {name}")
        if name in MEDIA_LOADERS:
            field = MEDIA_LOADERS[name]
            if not isinstance(inputs.get(field), str):
                raise ValueError("Media loader requires an input filename.")
            parts(inputs[field])
        # The pack accepts external FFmpeg templates; use known encoding fields.
        if name == "VHS_VideoCombine" and (
            inputs.get("format")
            not in {
                "video/h264-mp4",
                "video/h265-mp4",
                "video/webm",
                "image/gif",
                "image/webp",
            }
            or inputs.keys()
            - {
                "images",
                "frame_rate",
                "loop_count",
                "filename_prefix",
                "format",
                "pingpong",
                "save_output",
                "audio",
                "meta_batch",
                "vae",
                "pix_fmt",
                "crf",
                "save_metadata",
                "trim_to_audio",
                "dither",
            }
        ):
            raise ValueError("MCP requires a built-in video format and encoding fields.")
        reject_secrets(inputs)
        route = classification(name, definitions[name], inputs)
        if not route:
            continue
        values = {}
        schema = definitions[name].get("input", {})
        fields = {**schema.get("required", {}), **schema.get("optional", {})}
        for key, value in inputs.items():
            root = key.split(".", 1)[0]
            if schema and (
                root not in fields
                or (
                    key != root and fields[root][0] not in ("COMFY_DYNAMICCOMBO_V3", "COMFY_AUTOGROW_V3")
                )
            ):
                raise ValueError(f"{name}.{key}: 確認できない入力です。")
            # A provider may accept a remote file ID, history, or a literal filename
            # even without a graph edge. Only reviewed local loader edges carry media.
            if value and re.search(
                r"(file|uri|url|previous|history|interaction|conversation|persist_context|endpoint|reference_id)",
                key,
                re.IGNORECASE,
            ):
                raise ValueError(f"{name}.{key}: 外部ファイル・履歴や接続先の指定は確認できません。")
            if is_link(value):
                src = graph.get(value[0], {})
                loader = MEDIA_LOADERS.get(src.get("class_type"))
                if (
                    not loader
                    or value[1] != 0
                    or not isinstance(src.get("inputs", {}).get(loader), str)
                    or definitions.get(src["class_type"], {}).get("python_module")
                    not in {"nodes", "comfy_extras.nodes_audio", "comfy_extras.nodes_video"}
                    or set(src["inputs"]) != {loader}
                ):
                    raise ValueError(
                        f"{name}.{key}: 送信内容が実行まで確定しません。先にローカルで保存し、素材として渡してください。"
                    )
                filename = src["inputs"][loader]
                loaders[value[0]] = (loader, filename)
                values[key] = {"input_file": filename, "loader": src["class_type"]}
            elif (
                isinstance(value, dict)
                and key in {"model", "output_mode"}
                and name in {"OpenRouterText", "OpenRouterLLMNode", "OpenRouterImageNode"}
                and not (
                    value.keys()
                    - {
                        "model",
                        "model_id",
                        "reasoning_effort",
                        "search_context_size",
                        "output_mode",
                        "count",
                    }
                )
                and all(isinstance(v, (str, int, float, bool)) for v in value.values())
            ):
                values[key] = value
            elif isinstance(value, (dict, list)):
                raise ValueError(f"{name}.{key}: opaque nested input cannot be approved.")
            else:
                values[key] = value

        # Do not silently use a default model, prompt or hidden media field.
        required = definitions[name].get("input", {}).get("required", {})
        missing = required.keys() - inputs.keys()
        if missing:
            raise ValueError(f"{name}: 確認に必要な入力を指定してください: {', '.join(sorted(missing))}")
        calls.append(
            {
                "node_id": node_id,
                "class_type": name,
                "provider": route[0],
                "billing": route[1],
                "account": billing_account(route[1]),
                "inputs": values,
            }
        )
    return calls, loaders


def freeze_inputs(graph, loaders):
    """Freeze reviewed bytes into the same immutable-by-content loader names as upload_media."""
    root = Path(MOUNTS["input"])
    frozen = deepcopy(graph)
    assets = {}
    for node_id, (field, name) in loaders.items():
        if name not in assets:
            ext = Path(name).suffix.lower().lstrip(".")
            if ext not in MEDIA_EXT:
                raise ValueError("Unsupported external media. Upload a supported image, audio or video.")
            with regular(root, name) as f:
                size = os.fstat(f.fileno()).st_size
                if not 0 < size <= MAX_MEDIA:
                    raise ValueError("External media must be within the 256 MiB input limit.")
                digest = hashlib.file_digest(f, "sha256").hexdigest()
                transfer = input_media.Transfers(
                    root, hashlib.sha256(("approval:" + digest + ":" + ext).encode()).hexdigest()
                )
                info = transfer.apply("status", "review." + ext)
                if not info["complete"]:
                    f.seek(info["next_offset"])
                    while part := f.read(input_media.PART):
                        info = transfer.apply("part", offset=info["next_offset"], data=part)
                    info = transfer.apply("finish", total=size)
                if not info["filename"].startswith("mcp-" + digest + "."):
                    raise ValueError("Input changed during inspection.")
                assets[name] = {
                    "name": name,
                    "snapshot": info["filename"],
                    "size": size,
                    "sha256": digest,
                }
        frozen[node_id]["inputs"][field] = assets[name]["snapshot"]
    return frozen, list(assets.values())


def execution_guard(body, calls, assets):
    return {
        "digest": body_digest(body),
        "deployment": os.environ.get(DEPLOYMENT_ENV, ""),
        "routes": [
            [v[k] for k in ("node_id", "class_type", "provider", "billing", "account")] for v in calls
        ],
        "assets": assets,
    }


def verify_assets(assets):
    for item in assets:
        with regular(Path(MOUNTS["input"]), item["snapshot"]) as f:
            if hashlib.file_digest(f, "sha256").hexdigest() != item["sha256"]:
                raise ValueError("Approved media bytes have changed; job not executed.")
