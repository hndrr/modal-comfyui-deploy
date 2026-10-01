"""Place explicitly requested model assets from a JSON manifest into Modal Volume."""
from pathlib import Path
import json
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from preserve_model import app, preserve_model, COMFY_MODEL_SUBDIRS


def read_assets(manifest):
    data = json.loads(Path(manifest).read_text())
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('assets'), list):
        raise ValueError('Expected manifest version 1 with an assets list')
    if not data['assets']:
        raise ValueError('Manifest contains no assets')
    required = {'repo_id', 'revision', 'filename', 'destination_subdir'}
    for asset in data['assets']:
        if not isinstance(asset, dict) or not required <= asset.keys() or asset.keys() - required - {'expected_sha256'}:
            raise ValueError('Invalid asset fields')
        if any(not isinstance(v, str) or not v.strip() for v in asset.values()):
            raise ValueError('Asset fields must be nonempty strings')
        if asset['destination_subdir'] not in COMFY_MODEL_SUBDIRS:
            raise ValueError('Unknown model destination')
        if not re.fullmatch(r'[0-9a-f]{40}', asset['revision']):
            raise ValueError('Model revision must be a fixed commit')
        if 'expected_sha256' in asset and not re.fullmatch(r'[0-9a-f]{64}', asset['expected_sha256']):
            raise ValueError('Invalid SHA-256')
        for name in ('filename', 'destination_subdir'):
            path = Path(asset[name])
            if path.is_absolute() or '..' in path.parts:
                raise ValueError(f'{name} must be a relative path without parent traversal')
    return data['assets']


@app.local_entrypoint()
def prepare(manifest: str):
    # Validate the entire manifest before starting any download.
    for asset in read_assets(manifest):
        print(preserve_model.remote(**asset))
