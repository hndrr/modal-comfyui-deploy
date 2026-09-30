"""Shared Modal recipe fixtures; no legacy application import."""
from copy import deepcopy
from functools import partial
from ambient_comfyui.models import REF_MODELS
from ambient_comfyui.contracts import DEFAULT_BACKENDS as MODES
from uuid import uuid4
from ambient_comfyui.h3 import workflow as shared_workflow
from ambient_comfyui.models import MODAL_MODE_MODEL_FILES
from model_manifests import PREPARATION_MODES

DEFAULT_BACKENDS = PREPARATION_MODES
MODEL_FILES = MODAL_MODE_MODEL_FILES['h3']
FAST_MODEL_FILES = MODAL_MODE_MODEL_FILES['fasth3']
FAST8_MODEL_FILES = MODAL_MODE_MODEL_FILES['fasth3-8step-t2v']
FUSED_MODEL_FILES = MODAL_MODE_MODEL_FILES['h3-fused-4step']
workflow = partial(shared_workflow, profile='modal')

def request(**changes):
    return {'requestId': str(uuid4()), 'mode': 'h3', 'prompt': 'A quiet room',
            'sound': 'Soft breeze', 'seed': 42, 'resolution': 'preview', **changes}

def validate_object_info(info, mode='h3'):
    return workflow(request(mode=mode, sessionId=str(uuid4())),
                    'anchor.png' if mode not in {'fasth3', 'fasth3-8step-t2v'} else None, object_info=info)


def object_info():
    def node(inputs, outputs, **extra):
        return {
            "input": {"required": {name: [kind] for name, kind in inputs.items()}},
            "output": outputs,
            **extra,
        }

    info = {
        "UNETLoader": node(
            {"unet_name": [MODEL_FILES["unet"], FAST_MODEL_FILES["unet"], FAST8_MODEL_FILES["unet"], FUSED_MODEL_FILES["unet"]], "weight_dtype": ["default"]}, ["MODEL"]
        ),
        "LoraLoaderModelOnly": node(
            {"model": "MODEL", "lora_name": [MODEL_FILES["lora"]], "strength_model": "FLOAT"},
            ["MODEL"],
        ),
        "CLIPLoader": node(
            {"clip_name": [MODEL_FILES["clip"]], "type": ["minimax"], "device": ["default"]},
            ["CLIP"],
        ),
        "VAELoader": node(
            {"vae_name": list(dict.fromkeys((MODEL_FILES["video_vae"], MODEL_FILES["audio_vae"], FAST_MODEL_FILES["video_vae"])))}, ["VAE"]
        ),
        "LoadImage": node({"image": []}, ["IMAGE", "MASK"]),
        "MiniMaxH3ImageToVideo": node(
            {
                "clip": "CLIP",
                "vae": "VAE",
                "prompt": "STRING",
                "width": "INT",
                "height": "INT",
                "length": "INT",
            },
            ["CONDITIONING", "LATENT"],
        ),
        "BasicGuider": node({"model": "MODEL", "conditioning": "CONDITIONING"}, ["GUIDER"]),
        "RandomNoise": node({"noise_seed": "INT"}, ["NOISE"]),
        "KSamplerSelect": node({"sampler_name": ["res_multistep", "euler"]}, ["SAMPLER"]),
        "ManualSigmas": node({"sigmas": "STRING"}, ["SIGMAS"]),
        "MiniMaxH3SigmaShift": node(
            {"model": "MODEL", "shift_video": "FLOAT", "shift_audio": "FLOAT"}, ["MODEL"]
        ),
        "ModelAttentionBackend": node({"model": "MODEL", "attention": ["comfy kitchen attention"]}, ["MODEL"]),
        "BlockSparseAttention": node({
            "model": "MODEL", "start_percent": "FLOAT", "end_percent": "FLOAT",
            "dense_blocks": "STRING", "min_tokens": "INT", "extra_tokens": "INT",
            "sink_conditioning": ["exact_kv", "exact_kv_and_rows", "off"], "verbose": "BOOLEAN",
        }, ["MODEL"]),
        "BasicScheduler": node(
            {"model": "MODEL", "scheduler": ["simple"], "steps": "INT", "denoise": "FLOAT"},
            ["SIGMAS"],
        ),
        "SamplerCustomAdvanced": node(
            {
                "noise": "NOISE",
                "guider": "GUIDER",
                "sampler": "SAMPLER",
                "sigmas": "SIGMAS",
                "latent_image": "LATENT",
            },
            ["LATENT", "LATENT"],
            output_name=["output", "denoised_output"],
        ),
        "MiniMaxH3FastVAEDecode": node(
            {"samples": "LATENT", "vae": "VAE", "tile_batch_size": "INT"}, ["IMAGE"]
        ),
        "VAEDecodeAudio": node({"samples": "LATENT", "vae": "VAE"}, ["AUDIO"]),
        "CreateVideo": node({"images": "IMAGE", "audio": "AUDIO", "fps": "FLOAT"}, ["VIDEO"]),
        "SaveVideo": node(
            {"video": "VIDEO", "filename_prefix": "STRING", "format": ["mp4"], "codec": ["h264"]},
            [],
            output_node=True,
        ),
    }
    info["LoadImage"]["input"]["required"]["image"].append({"image_upload": True})
    info["BlockSparseAttention"]["input"]["required"]["selection"] = [
        "COMFY_DYNAMICCOMBO_V3", {"options": [
            {"key": "sol-attn", "inputs": {"required": {"tau": ["FLOAT", {"default": 1.3}]}}},
            {"key": "vsa", "inputs": {"required": {"keep_percent": ["FLOAT", {"default": 10.0}]}}},
        ]},
    ]
    info["MiniMaxH3ImageToVideo"]["input"]["optional"] = {
        "first_frame": ["IMAGE"],
        "last_frame": ["IMAGE"],
    }
    return info


def recipe_objects():
    info = object_info()
    def node(inputs, outputs):
        return {"input": {"required": {k: [v] for k, v in inputs.items()}}, "output": outputs}
    info["UNETLoader"]["input"]["required"]["unet_name"][0].extend(REF_MODELS)
    info["MiniMaxH3ImageToVideo"]["input"]["required"]["length"] = ["INT", {"default": 124, "min": 5, "max": 3600}]
    info["VAEDecode"] = node({"samples": "LATENT", "vae": "VAE"}, ["IMAGE"])
    info["EasyCache"] = node({"model": "MODEL", "reuse_threshold": "FLOAT", "start_percent": "FLOAT", "end_percent": "FLOAT", "verbose": "BOOLEAN"}, ["MODEL"])
    info["SolAttnMiniMax"] = node({"model": "MODEL", "start_percent": "FLOAT", "end_percent": "FLOAT", "min_tokens": "INT", "sink_conditioning": ["exact_kv_and_rows"], "verbose": "BOOLEAN"}, ["MODEL"])
    info["SolAttnMiniMax"]["input"]["required"]["selection"] = ["COMFY_DYNAMICCOMBO_V3", {"options": [
        {"key": "VSA (FastVideo)", "inputs": {"required": {"vsa_keep_percent": ["FLOAT", {"default": 10.0}]}}}]}]
    ref = info["MiniMaxH3ReferenceToVideo"] = deepcopy(info["MiniMaxH3ImageToVideo"])
    ref["input"]["required"]["ref_image_size"] = [["match", "max"]]
    ref["input"]["optional"] = {"audio_vae": ["VAE"], "ref_images": ["COMFY_AUTOGROW_V3", {"template": {
        "prefix": "ref_image_", "min": 1, "max": 9, "input": {"optional": {"image": ["IMAGE"]}}}}]}
    return info
