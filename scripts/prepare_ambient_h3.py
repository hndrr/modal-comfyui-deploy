"""Explicit model preparation; never runs on app startup or deploy.
Run: ./scripts/modal.sh run scripts/prepare_ambient_h3.py
"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from preserve_model import app, preserve_model
from ambient.h3 import MODEL_FILES
from ambient.config import H3_MODEL_REVISION

@app.local_entrypoint()
def prepare():
    for key, subdir in [('unet', 'diffusion_models'), ('clip', 'text_encoders'), ('video_vae', 'vae'), ('audio_vae', 'vae'), ('lora', 'loras')]:
        filename = f'{subdir}/{MODEL_FILES[key]}'
        print(preserve_model.remote(repo_id='Comfy-Org/MiniMax-H3', filename=filename,
                                    revision=H3_MODEL_REVISION, destination_subdir=subdir))
