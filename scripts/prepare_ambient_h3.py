"""Explicit model preparation; never runs on app startup or deploy.
Run: ./scripts/modal.sh run scripts/prepare_ambient_h3.py --mode h3|fasth3
"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from preserve_model import app, preserve_model
from ambient.models import comfy_assets

@app.local_entrypoint()
def prepare(mode: str = "h3"):
    for asset in comfy_assets(mode):
        print(preserve_model.remote(**asset))
