"""Validate native workflow inputs/models without launching a generation."""
from .comfy import MODEL_FILES, workflow
from .contracts import identifier

def validate_object_info(info):
    required = {node['class_type'] for node in workflow({'requestId': '00000000-0000-4000-8000-000000000001', 'prompt': 'test', 'sound': 'test', 'seed': 1, 'resolution': 'preview'}, 'anchor.png').values()}
    missing = sorted(required - info.keys())
    if missing:
        raise ValueError('Update ComfyUI; missing native nodes: '+', '.join(missing))
    def choices(node, field):
        fields = {**info[node]['input'].get('required', {}), **info[node]['input'].get('optional', {})}
        spec = fields.get(field, [])
        return spec[0] if spec and isinstance(spec[0], list) else []
    for node, field, name in [('UNETLoader', 'unet_name', 'unet'), ('CLIPLoader', 'clip_name', 'clip'), ('VAELoader', 'vae_name', 'video_vae'), ('VAELoader', 'vae_name', 'audio_vae'), ('LoraLoaderModelOnly', 'lora_name', 'lora')]:
        if MODEL_FILES[name] not in choices(node, field):
            raise ValueError('Missing model: '+MODEL_FILES[name])
    if 'minimax' not in choices('CLIPLoader', 'type'):
        raise ValueError('ComfyUI CLIPLoader has no minimax type')
    if 'res_multistep' not in choices('KSamplerSelect', 'sampler_name'):
        raise ValueError('ComfyUI has no res_multistep sampler')
    fields = info['MiniMaxH3ImageToVideo']['input']
    if 'first_frame' not in {**fields.get('required', {}), **fields.get('optional', {})}:
        raise ValueError('H3 native node has no first_frame input')
    return True
