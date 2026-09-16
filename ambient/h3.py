"""Ambient's H3/FastH3 recipes, bound to ComfyUI's public node definitions.

This builds API inputs only. ComfyUI owns node implementations, final prompt
validation and execution; no ComfyUI source or runtime method is patched here.
"""

from copy import deepcopy
from dataclasses import dataclass

from .contracts import FPS, FRAMES, RESOLUTIONS, prompt_text
from .models import FAST_MODEL_FILES, MODEL_FILES

FAST_SIGMAS = (1.0, 36 / 37, 12 / 13, 4 / 5, 0.0)


def input_fields(schema, values):
    """Bind the selected DynamicCombo branch to ComfyUI's dotted API input names."""
    required = dict(schema.get("required", {}))
    fields = {**required, **schema.get("optional", {})}
    for name, spec in list(fields.items()):
        if spec[0] != "COMFY_DYNAMICCOMBO_V3":
            continue
        if name not in values and name not in required:
            continue
        options = spec[1].get("options", [])
        selected = next(
            (option for option in options if option["key"] == values.get(name)), None
        )
        if selected is None:
            raise ValueError(
                f"ComfyUI {name} requires an available DynamicCombo selection"
            )
        prefix = name + "."
        child_values = {
            key[len(prefix) :]: value
            for key, value in values.items()
            if key.startswith(prefix)
        }
        child_required, child_fields = input_fields(selected["inputs"], child_values)
        required.update({prefix + key: value for key, value in child_required.items()})
        fields.update({prefix + key: value for key, value in child_fields.items()})
    return required, fields


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
                f"ComfyUI is missing node {kind}; check the installed H3 nodes"
            )
        definition = self.objects[kind]
        required, fields = input_fields(definition["input"], values)
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


def workflow(
    request: dict, image_name: str | None = None, *, object_info: dict
) -> dict:
    """Keep Ambient's creative settings while inheriting the server's node contracts."""
    mode = request.get("mode", "h3")
    if mode not in ("h3", "fasth3"):
        raise ValueError("Unsupported ComfyUI generation mode")
    fast = mode == "fasth3"
    if fast and (image_name or request.get("imageId") or request.get("parentClipId")):
        raise ValueError("FastH3 Preview supports text-to-video-and-audio only")
    files = FAST_MODEL_FILES if fast else MODEL_FILES
    width, height = RESOLUTIONS[request["resolution"]]
    builder = Workflow(object_info)
    add = builder.add
    add("1", "UNETLoader", unet_name=files["unet"], weight_dtype="default")
    if fast:
        add(
            "17",
            "MiniMaxH3SigmaShift",
            model=Link("1"),
            shift_video=12.0,
            shift_audio=3.0,
        )
        add(
            "2",
            "BlockSparseAttention",
            model=Link("17"),
            selection="vsa",
            **{"selection.keep_percent": 10.0},
            start_percent=0.0,
            end_percent=1.0,
            dense_blocks="",
            min_tokens=0,
            extra_tokens=0,
            sink_conditioning="exact_kv_and_rows",
            verbose=True,
        )
    else:
        add(
            "2",
            "LoraLoaderModelOnly",
            model=Link("1"),
            lora_name=files["lora"],
            strength_model=1.0,
        )
    add("3", "CLIPLoader", clip_name=files["clip"], type="minimax", device="default")
    add("4", "VAELoader", vae_name=files["video_vae"])
    add("5", "VAELoader", vae_name=files["audio_vae"])
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
    add("9", "KSamplerSelect", sampler_name="euler" if fast else "res_multistep")
    if fast:
        add("10", "ManualSigmas", sigmas=", ".join(map(str, FAST_SIGMAS)))
    else:
        add(
            "10",
            "BasicScheduler",
            model=Link("2"),
            scheduler="simple",
            steps=8,
            denoise=1.0,
        )
    add(
        "11",
        "SamplerCustomAdvanced",
        noise=Link("8"),
        guider=Link("7"),
        sampler=Link("9"),
        sigmas=Link("10"),
        latent_image=Link("6"),
    )
    add(
        "12", "MiniMaxH3FastVAEDecode",
        samples=Link("11", "output"), vae=Link("4"), tile_batch_size=4,
    )
    add("13", "VAEDecodeAudio", samples=Link("11", "output"), vae=Link("5"))
    add("14", "CreateVideo", images=Link("12"), audio=Link("13"), fps=FPS)
    format_spec = (
        object_info.get("SaveVideo", {}).get("input", {}).get("required", {}).get("format", [])
    )
    # Current SaveVideo nests the codec under the container's DynamicCombo.
    codec_field = "format.codec" if format_spec[:1] == ["COMFY_DYNAMICCOMBO_V3"] else "codec"
    add(
        "15",
        "SaveVideo",
        video=Link("14"),
        filename_prefix=f"ambient/raw/{request['requestId']}",
        format="mp4",
        **{codec_field: "h264"},
    )
    if not object_info["SaveVideo"].get("output_node"):
        raise ValueError(
            "ComfyUI SaveVideo is not an output node; update the H3 adapter"
        )
    return builder.graph
