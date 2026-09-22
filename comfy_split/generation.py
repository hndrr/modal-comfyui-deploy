"""Split-only video recipes; the legacy Ambient API keeps its existing modes."""

from ambient.contracts import DEFAULT_BACKENDS, IMAGE_MODES as LEGACY_IMAGE_MODES, FRAMES, FPS, generation_size, prompt_text
from ambient.h3 import Link, Workflow, workflow as legacy_workflow
from ambient.models import FAST8_MODEL_FILES

NEW_MODES = ("h3-ref2v", "fasth3-8step-i2v-vsa", "fasth3-vsa-4step-i2v")
MODES = (*DEFAULT_BACKENDS, *NEW_MODES)
I2V_MODES = {"fasth3-8step-i2v", *NEW_MODES[1:]}
IMAGE_MODES = LEGACY_IMAGE_MODES | I2V_MODES
REF_MODELS = ("minimax_h3_ref2va_pruned_int8_convrot.safetensors", "minimax_h3_ref2va_int8_convrot.safetensors")


def workflow(request, image_name=None, *, object_info):
    mode = request.get("mode", "h3")
    if mode not in MODES:
        raise ValueError("Unsupported Split generation mode")
    frames = request.get("frames", FRAMES)
    if type(frames) is not int or frames < 5 or frames % 17 != 5:
        raise ValueError("Length must be a positive 17k+5 frame count")
    kind = "MiniMaxH3ReferenceToVideo" if mode == "h3-ref2v" else "MiniMaxH3ImageToVideo"
    spec = object_info.get(kind, {}).get("input", {}).get("required", {}).get("length", [])
    limits = spec[1] if len(spec) > 1 else {}
    if frames < limits.get("min", 5) or frames > limits.get("max", 3600):
        raise ValueError("Length is outside the connected node's range")
    if mode in DEFAULT_BACKENDS:
        graph = legacy_workflow(request, image_name, object_info=object_info)
        graph["6"]["inputs"]["length"] = frames
        return graph

    ref = mode == "h3-ref2v"
    sol = mode == "fasth3-vsa-4step-i2v"
    names = request.get("referenceNames", ["ambient/reference.png"])
    if ref and (not isinstance(names, list) or not 1 <= len(names) <= 9
                or any(not isinstance(name, str) or not name for name in names)):
        raise ValueError("Ref2V requires 1–9 ordered images")
    if ref and image_name:
        raise ValueError("Ref2V uses referenceNames, not a first-frame image")
    if not ref and not image_name and not request.get("sessionId"):
        raise ValueError("I2V requires a first-frame image")
    files = dict(FAST8_MODEL_FILES)
    if ref:
        choices = object_info.get("UNETLoader", {}).get("input", {}).get("required", {}).get("unet_name", [[]])[0]
        files["unet"] = next((name for name in REF_MODELS if name in choices), REF_MODELS[0])
    width, height = generation_size(request)
    builder = Workflow(object_info)
    add = builder.add
    add("1", "UNETLoader", unet_name=files["unet"], weight_dtype="default")
    if ref:
        add("17", "EasyCache", model=Link("1"), reuse_threshold=0.3, start_percent=0.2, end_percent=0.9, verbose=False)
        add("2", "ModelAttentionBackend", model=Link("17"), attention="comfy kitchen attention")
    else:
        add("17", "MiniMaxH3SigmaShift", model=Link("1"), shift_video=12.0 if sol else 10.0, shift_audio=3.0)
        if sol:
            add("2", "SolAttnMiniMax", model=Link("17"), selection="VSA (FastVideo)",
                **{"selection.vsa_keep_percent": 10.0}, start_percent=0.0, end_percent=1.0,
                min_tokens=0, sink_conditioning="exact_kv_and_rows", verbose=True)
        else:
            add("18", "ModelAttentionBackend", model=Link("17"), attention="comfy kitchen attention")
            add("2", "BlockSparseAttention", model=Link("18"), selection="vsa",
                **{"selection.keep_percent": 10.0}, start_percent=0.2, end_percent=1.0,
                dense_blocks="", min_tokens=12288, extra_tokens=256,
                sink_conditioning="exact_kv_and_rows", verbose=False)
    add("3", "CLIPLoader", clip_name=files["clip"], type="minimax", device="default")
    add("4", "VAELoader", vae_name=files["video_vae"])
    add("5", "VAELoader", vae_name=files["audio_vae"])
    anchor = {}
    if ref:
        for index, name in enumerate(names):
            node = f"ambient_ref_{index}"
            add(node, "LoadImage", image=name)
            anchor[f"ref_images.ref_image_{index}"] = Link(node)
        anchor.update(audio_vae=Link("5"), ref_image_size="match")
    else:
        add("16", "LoadImage", image=image_name or "")
        anchor["first_frame"] = Link("16")
    add("6", kind, clip=Link("3"), vae=Link("4"), prompt=prompt_text(request),
        width=width, height=height, length=frames, **anchor)
    add("7", "BasicGuider", model=Link("2"), conditioning=Link("6"))
    add("8", "RandomNoise", noise_seed=request["seed"])
    add("9", "KSamplerSelect", sampler_name="euler" if sol else "res_multistep")
    if sol:
        add("10", "ManualSigmas", sigmas="0.9999166, 0.9728326, 0.9230769, 0.8, 0.0")
    else:
        add("10", "BasicScheduler", model=Link("2"), scheduler="simple", steps=20 if ref else 8, denoise=1.0)
    add("11", "SamplerCustomAdvanced", noise=Link("8"), guider=Link("7"),
        sampler=Link("9"), sigmas=Link("10"), latent_image=Link("6"))
    add("12", "MiniMaxH3FastVAEDecode" if sol else "VAEDecode",
        samples=Link("11", "output"), vae=Link("4"), **({"tile_batch_size": 4} if sol else {}))
    add("13", "VAEDecodeAudio", samples=Link("11", "output"), vae=Link("5"))
    add("14", "CreateVideo", images=Link("12"), audio=Link("13"), fps=FPS)
    codec = "format.codec" if object_info.get("SaveVideo", {}).get("input", {}).get("required", {}).get("format", [])[:1] == ["COMFY_DYNAMICCOMBO_V3"] else "codec"
    add("15", "SaveVideo", video=Link("14"), filename_prefix=f"ambient/raw/{request['requestId']}", format="mp4", **{codec: "h264"})
    if not object_info["SaveVideo"].get("output_node"):
        raise ValueError("ComfyUI SaveVideo is not an output node")
    return builder.graph
