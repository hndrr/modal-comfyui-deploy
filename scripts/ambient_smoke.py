"""Explicit paid GPU smoke test. Never imported/run by the frontend or a test suite."""
import argparse
import json
import os
from pathlib import Path
import time
from urllib.request import Request, urlopen
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['h3', 'fasth3'], required=True)
    parser.add_argument('--clips', type=int, default=3)
    parser.add_argument('--output', default='ambient-smoke')
    args = parser.parse_args()
    if not 1 <= args.clips <= 10: parser.error('--clips must be between 1 and 10')
    base = os.environ['AMBIENT_BACKEND_URL'].rstrip('/')
    headers = {'Modal-Key': os.environ['MODAL_PROXY_KEY'], 'Modal-Secret': os.environ['MODAL_PROXY_SECRET']}
    def call(path, data=None):
        req = Request(base+path, data=json.dumps(data).encode() if data else None, headers={**headers, 'Content-Type': 'application/json'})
        with urlopen(req, timeout=120) as response: return json.load(response)
    parent = None; directory = Path(args.output); directory.mkdir(parents=True, exist_ok=True)
    for index in range(args.clips):
        req = dict(requestId=str(uuid4()), mode=args.mode, prompt='A quiet sunlit room with curtains moving gently in a breeze. A still camera, continuous shot, no cuts.', sound='Continuous room tone, soft breeze and distant leaves, sustained ambient tone, no speech, no percussion.', seed=42+index, resolution='preview')
        if parent and args.mode == 'h3': req['parentClipId'] = parent
        job = call('/jobs', req); deadline = time.monotonic()+3600
        while job['status'] not in ('completed', 'failed', 'cancelled'):
            if time.monotonic() > deadline: raise TimeoutError(f"Inspect job {job['id']}; do not re-submit")
            time.sleep(3); job = call('/jobs/'+job['id']); print(job['id'], job.get('stage', job['status']))
        if job['status'] != 'completed' or not job['clip']['hasAudio']: raise RuntimeError(job)
        with urlopen(Request(base+'/clips/'+job['id'], headers=headers), timeout=120) as response:
            (directory/f'{index+1:02}-{job["id"]}.mp4').write_bytes(response.read())
        (directory/f'{index+1:02}.json').write_text(json.dumps({'request': req, 'result': job}, indent=2))
        parent = job['id']
    print(f'Saved {args.clips} native audio/video clips to {directory}. Listen and inspect the joins.')

if __name__ == '__main__': main()
