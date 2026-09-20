"""Operate Ambient jobs without a browser. Generate/submit invoke the configured GPU backend."""

import argparse
from http.client import HTTPException
import json
from pathlib import Path
import sys
from uuid import uuid4

from .client import Client, save_request
from .contracts import ASPECT_RATIOS, DEFAULT_BACKENDS, IMAGE_MODES, RESOLUTIONS, identifier, validate_request


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Must be positive")
    return number


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "capabilities", help="Read preparation status without starting a GPU"
    )
    for name in ("status", "cancel", "download"):
        command = commands.add_parser(name)
        command.add_argument("job_id", type=identifier)
        if name == "download":
            command.add_argument("--output", type=Path, default=Path("ambient-output"))
    generate = commands.add_parser(
        "generate", help="Submit, wait, and save an audio/video clip"
    )
    generate.add_argument("--mode", choices=list(DEFAULT_BACKENDS), required=True)
    generate.add_argument("--backend", choices=["comfyui"], default="comfyui")
    generate.add_argument("--prompt", required=True)
    generate.add_argument("--sound", required=True)
    generate.add_argument("--seed", type=int, default=42)
    generate.add_argument("--resolution", choices=list(RESOLUTIONS), default="preview")
    generate.add_argument("--aspect-ratio", choices=list(ASPECT_RATIOS), default="9:16")
    generate.add_argument("--request-id", type=identifier)
    anchor = generate.add_mutually_exclusive_group()
    anchor.add_argument("--image", type=Path)
    anchor.add_argument("--parent-clip-id", type=identifier)
    submit = commands.add_parser(
        "submit", help="Resend a saved request with the same ID and body"
    )
    submit.add_argument("request_file", type=Path)
    for command in (generate, submit):
        command.add_argument("--output", type=Path, default=Path("ambient-output"))
        command.add_argument("--timeout", type=positive, default=3600)
    return root


def progress(job):
    print(job["id"], job.get("stage", job["status"]), file=sys.stderr, flush=True)


def main(argv=None, *, client=None):
    args = parser().parse_args(argv)
    job_id = None
    posted = False
    try:
        # Parse and validate local inputs before credentials or network access.
        if args.command == "generate":
            request = {
                "requestId": args.request_id or str(uuid4()),
                "mode": args.mode,
                "backend": args.backend,
                "prompt": args.prompt,
                "sound": args.sound,
                "seed": args.seed,
                "resolution": args.resolution,
                "aspectRatio": args.aspect_ratio,
            }
            if args.parent_clip_id:
                request["parentClipId"] = args.parent_clip_id
            if args.image and args.mode not in IMAGE_MODES:
                raise ValueError("FastH3 Preview supports text-to-video-and-audio only")
            # Reserve a syntactically valid ID for local validation; replace it
            # with the upload result before saving or submitting the request.
            request = validate_request({**request, **({"imageId": request["requestId"]} if args.image else {})})
        elif args.command == "submit":
            request = validate_request(json.loads(args.request_file.read_text()))
        client = client or Client.from_env()
        if args.command == "capabilities":
            result = client.capabilities()
        elif args.command in ("status", "cancel"):
            result = getattr(client, args.command)(args.job_id)
        elif args.command == "download":
            result = {"path": str(client.download(args.job_id, args.output))}
        else:
            if args.command == "generate" and args.image:
                request["imageId"] = client.upload(args.image)
            saved = save_request(request, args.output)
            job_id = request["requestId"]
            print(f"Request {job_id} saved to {saved}", file=sys.stderr, flush=True)
            # A lost response can still mean the server accepted this request.
            posted = True
            job = client.wait(
                client.submit(request), timeout=args.timeout, progress=progress
            )
            if job["status"] != "completed" or not job.get("clip", {}).get("hasAudio"):
                raise RuntimeError(f"Job {job_id}: {job.get('error', job['status'])}")
            result = {
                "job": job,
                "path": str(client.download(job_id, args.output, job)),
            }
        print(json.dumps(result, indent=2))
        return 0
    except KeyboardInterrupt:
        if posted:
            try:
                result = client.cancel(job_id)
                print(
                    f"Cancel response for {job_id}: {result['status']} (GPU exit is asynchronous)",
                    file=sys.stderr,
                )
            except Exception as error:
                print(f"Cancel not confirmed for {job_id}: {error}", file=sys.stderr)
        return 130
    except (OSError, HTTPException, ValueError, RuntimeError, KeyError) as error:
        print(f"Ambient: {error}", file=sys.stderr)
        if posted:
            print(
                f"Inspect with: python -m ambient.cli status {job_id}\n"
                f"Resend only the saved body: python -m ambient.cli submit {saved}",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
