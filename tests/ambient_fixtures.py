"""Small /object_info double for adapter tests; not a GPU compatibility claim."""

from ambient.h3 import FAST8_MODEL_FILES, FAST_MODEL_FILES, MODEL_FILES
from ambient.models import FUSED_MODEL_FILES


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
            {"vae_name": [MODEL_FILES["video_vae"], MODEL_FILES["audio_vae"], FAST_MODEL_FILES["video_vae"]]}, ["VAE"]
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
