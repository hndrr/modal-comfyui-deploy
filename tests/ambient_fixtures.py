"""Small /object_info double for adapter tests; not a GPU compatibility claim."""

from ambient.h3 import MODEL_FILES


def object_info():
    def node(inputs, outputs, **extra):
        return {
            "input": {"required": {name: [kind] for name, kind in inputs.items()}},
            "output": outputs,
            **extra,
        }

    info = {
        "UNETLoader": node(
            {"unet_name": [MODEL_FILES["unet"]], "weight_dtype": ["default"]}, ["MODEL"]
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
            {"vae_name": [MODEL_FILES["video_vae"], MODEL_FILES["audio_vae"]]}, ["VAE"]
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
        "KSamplerSelect": node({"sampler_name": ["res_multistep"]}, ["SAMPLER"]),
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
        "VAEDecode": node({"samples": "LATENT", "vae": "VAE"}, ["IMAGE"]),
        "VAEDecodeAudio": node({"samples": "LATENT", "vae": "VAE"}, ["AUDIO"]),
        "CreateVideo": node({"images": "IMAGE", "audio": "AUDIO", "fps": "FLOAT"}, ["VIDEO"]),
        "SaveVideo": node(
            {"video": "VIDEO", "filename_prefix": "STRING", "format": ["mp4"], "codec": ["h264"]},
            [],
            output_node=True,
        ),
    }
    info["LoadImage"]["input"]["required"]["image"].append({"image_upload": True})
    info["MiniMaxH3ImageToVideo"]["input"]["optional"] = {
        "first_frame": ["IMAGE"],
        "last_frame": ["IMAGE"],
    }
    return info
