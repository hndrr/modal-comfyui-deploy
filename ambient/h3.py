"""Ambient's H3 recipe, bound to the running ComfyUI's public node definitions.

This builds API inputs only. ComfyUI owns node implementations, final prompt
validation and execution; no ComfyUI source or runtime method is patched here.
"""

from copy import deepcopy
from dataclasses import dataclass

from .contracts import FPS, FRAMES, RESOLUTIONS, prompt_text

MODEL_FILES = {
    "unet": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    "clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "video_vae": "minimax_h3_video_vae_fp16.safetensors",
    "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
    "lora": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
}


@dataclass(frozen=True)
class Link:
    node: str
    output_name: str | None = None


class Workflow:
    """Resolve connections and explicit defaults from /object_info, never slot guesses."""

    def __init__(self, objects):
        self.objects = objects
        self.graph = {}

    def add(self, node_id, kind, **values):
        if kind not in self.objects:
            raise ValueError(
                f"ComfyUI is missing native node {kind}; check the upstream H3 workflow"
            )
        definition = self.objects[kind]
        required = definition["input"].get("required", {})
        fields = {**required, **definition["input"].get("optional", {})}
        for field in values.keys() - fields.keys():
            raise ValueError(
                f"ComfyUI {kind}.{field} is no longer available; update the H3 adapter"
            )
        for field, spec in required.items():
            if field not in values:
                options = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
                if "default" not in options:
                    raise ValueError(
                        f"ComfyUI {kind}.{field} requires an explicit input; update the H3 adapter"
                    )
                values[field] = deepcopy(options["default"])
        for field, value in values.items():
            spec = fields[field]
            options = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            if isinstance(value, Link):
                source = self.objects[self.graph[value.node]["class_type"]]
                outputs = source.get("output", [])
                names = source.get("output_name", outputs)
                matches = [
                    index
                    for index, output in enumerate(outputs)
                    if output == spec[0]
                    and (
                        value.output_name is None
                        or (index < len(names) and names[index] == value.output_name)
                    )
                ]
                if len(matches) != 1:
                    raise ValueError(
                        f"ComfyUI output for {kind}.{field} is missing or ambiguous; update the H3 adapter"
                    )
                values[field] = [value.node, matches[0]]
            elif (
                isinstance(spec[0], list)
                and value not in spec[0]
                and not options.get("image_upload")
            ):
                raise ValueError(
                    f"ComfyUI {kind}.{field} does not offer {value!r}; check its models and node version"
                )
        self.graph[node_id] = {"class_type": kind, "inputs": values}


def workflow(request: dict, image_name: str | None = None, *, object_info: dict) -> dict:
    """Keep Ambient's creative settings while inheriting the server's node contracts."""
    width, height = RESOLUTIONS[request["resolution"]]
    builder = Workflow(object_info)
    add = builder.add
    add("1", "UNETLoader", unet_name=MODEL_FILES["unet"], weight_dtype="default")
    add(
        "2",
        "LoraLoaderModelOnly",
        model=Link("1"),
        lora_name=MODEL_FILES["lora"],
        strength_model=1.0,
    )
    add("3", "CLIPLoader", clip_name=MODEL_FILES["clip"], type="minimax", device="default")
    add("4", "VAELoader", vae_name=MODEL_FILES["video_vae"])
    add("5", "VAELoader", vae_name=MODEL_FILES["audio_vae"])
    anchor = {}
    if image_name:
        add("16", "LoadImage", image=image_name)
        anchor["first_frame"] = Link("16")
    add(
        "6",
        "MiniMaxH3ImageToVideo",
        clip=Link("3"),
        vae=Link("4"),
        prompt=prompt_text(request),
        width=width,
        height=height,
        length=FRAMES,
        **anchor,
    )
    add("7", "BasicGuider", model=Link("2"), conditioning=Link("6"))
    add("8", "RandomNoise", noise_seed=request["seed"])
    add("9", "KSamplerSelect", sampler_name="res_multistep")
    add("10", "BasicScheduler", model=Link("2"), scheduler="simple", steps=8, denoise=1.0)
    add(
        "11",
        "SamplerCustomAdvanced",
        noise=Link("8"),
        guider=Link("7"),
        sampler=Link("9"),
        sigmas=Link("10"),
        latent_image=Link("6"),
    )
    add("12", "VAEDecode", samples=Link("11", "output"), vae=Link("4"))
    add("13", "VAEDecodeAudio", samples=Link("11", "output"), vae=Link("5"))
    add("14", "CreateVideo", images=Link("12"), audio=Link("13"), fps=FPS)
    add(
        "15",
        "SaveVideo",
        video=Link("14"),
        filename_prefix=f"ambient/raw/{request['requestId']}",
        format="mp4",
        codec="h264",
    )
    if not object_info["SaveVideo"].get("output_node"):
        raise ValueError("ComfyUI SaveVideo is not an output node; update the H3 adapter")
    return builder.graph
