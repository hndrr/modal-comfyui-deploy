"""Explicit paid GPU smoke test. Never imported/run by the frontend or a test suite."""

import argparse
import json
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ambient.client import Client, save_request
from ambient.cli import progress
from ambient.contracts import DEFAULT_BACKENDS, validate_request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["h3", "fasth3"], required=True)
    parser.add_argument("--backend", choices=["comfyui", "fastvideo"])
    parser.add_argument("--clips", type=int, default=3)
    parser.add_argument("--output", default="ambient-smoke")
    args = parser.parse_args()
    if not 1 <= args.clips <= 10:
        parser.error("--clips must be between 1 and 10")
    template = validate_request(dict(
        requestId=str(uuid4()), mode=args.mode,
        backend=args.backend or DEFAULT_BACKENDS[args.mode],
        prompt="A quiet sunlit room with curtains moving gently in a breeze. A still camera, continuous shot, no cuts.",
        sound="Continuous room tone, soft breeze and distant leaves, sustained ambient tone, no speech, no percussion.",
        seed=42, resolution="preview",
    ))
    client = Client.from_env()
    parent = None
    directory = Path(args.output)
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(args.clips):
        req = {**template, "requestId": str(uuid4()), "seed": 42 + index}
        if parent and args.mode == "h3":
            req["parentClipId"] = parent
        saved = save_request(req, directory)
        print(f"Request {req['requestId']} saved to {saved}", flush=True)
        try:
            job = client.wait(client.submit(req), progress=progress)
            if job["status"] != "completed" or not job["clip"]["hasAudio"]:
                raise RuntimeError(job)
            path = client.download(job["id"], directory, job)
            path.replace(directory / f"{index + 1:02}-{job['id']}.mp4")
            (directory / f"{index + 1:02}.json").write_text(
                json.dumps({"request": req, "result": job}, indent=2) + "\n"
            )
        except KeyboardInterrupt:
            print(f"Cancel response: {client.cancel(req['requestId'])}", file=sys.stderr)
            raise
        parent = job["id"]
    print(f"Saved {args.clips} native audio/video clips to {directory}. Listen and inspect the joins.")


if __name__ == "__main__":
    main()
