"""Run inside the Modal CPU container; no GPU calls or environment mutations.

Verify normal upstream startup without the bridge and CPU's bridge guard/API.
"""
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def get(origin, path):
    with urllib.request.urlopen(origin + path, timeout=5) as response:
        return json.load(response)


def main():
    origin = 'http://127.0.0.1:8000'
    state = get(origin, '/split/status')
    assert get('http://127.0.0.1:8187', '/_split/catalog')['cpu_guard']
    before = get(origin, '/api/history')
    jobs = get(origin, '/api/jobs?limit=100')
    assert jobs['pagination']['total'] >= len(before)
    with tempfile.TemporaryDirectory(prefix='plain-comfy-') as directory:
        root = Path(directory)
        for folder in ('custom_nodes', 'input', 'output', 'user'):
            (root / folder).mkdir()
        interpreter = f'/environments/{state["environment"]}/venv/bin/python'
        env = dict(os.environ, SPLIT_INTEGRATION='0', SPLIT_CPU='0')
        with (root / 'server.log').open('w') as log:
            process = subprocess.Popen([interpreter, '/opt/comfy-template/main.py', '--cpu',
                '--listen', '127.0.0.1', '--port', '8196', '--base-directory', str(root),
                '--user-directory', str(root / 'user'), '--temp-directory', str(root / 'temporary'),
                '--database-url', 'sqlite:///' + str(root / 'plain.db'), '--disable-all-custom-nodes'],
                cwd='/opt/comfy-template', env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                plain = 'http://127.0.0.1:8196'
                for _ in range(90):
                    if process.poll() is not None:
                        raise RuntimeError((root / 'server.log').read_text()[-4000:])
                    try:
                        nodes = get(plain, '/object_info')
                        break
                    except (OSError, TimeoutError):
                        time.sleep(1)
                else:
                    raise RuntimeError('Normal ComfyUI startup timeout')
                assert 'EmptyImage' in nodes
                try:
                    get(plain, '/_split/catalog')
                except urllib.error.HTTPError as error:
                    assert error.code == 404
                else:
                    raise AssertionError('Bridge is unexpectedly enabled')
                body = {'prompt': {
                    '1': {'class_type': 'EmptyImage', 'inputs': {'width': 64, 'height': 64, 'batch_size': 1, 'color': 0}},
                    '2': {'class_type': 'SaveImage', 'inputs': {'images': ['1', 0], 'filename_prefix': 'plain'}}}}
                request = urllib.request.Request(plain + '/prompt', data=json.dumps(body).encode(),
                                                 headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(request) as response:
                    job = json.load(response)['prompt_id']
                for _ in range(30):
                    history = get(plain, '/history/' + job)
                    if history:
                        assert history[job]['status']['status_str'] == 'success'
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError('Normal CPU execution failed')
                print(json.dumps({'plain_startup': True, 'bridge_absent': True,
                                  'normal_cpu_execution': True, 'retained_history': len(before)}), flush=True)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == '__main__':
    main()
