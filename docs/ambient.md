# H3 Ambient backend

`ambient_app.py` adds a separate CPU job API and FastVideo GPU worker to this repository. The existing `comfyapp.py`, model tools and asset UI remain independent. Deploying or opening the frontend does not download models. The frontend lives in `comfy-stream` at `/ambient`.

**Current validation:** local contract, media and mocked ComfyUI tests only. No deployment or GPU generation was performed. ComfyUI/FastVideo/model references below are reproducible source references, not GPU-qualified releases. GPU generation, three-clip visual continuity and actual model performance remain to be checked when GPU use is explicitly resumed.

## Configuration

Use the repository's `.env` and pinned `.modal-profile`. Do not put credentials in `NEXT_PUBLIC_*` variables.

```dotenv
AMBIENT_COMFYUI_URL=https://YOUR-EXISTING-COMFYUI-ENDPOINT.modal.run
MODAL_PROXY_KEY=...
MODAL_PROXY_SECRET=...
AMBIENT_FASTH3_MODEL_REVISION=5ea076f35b84da4c3c82217112fa733d8eea2ae1
# Optional exact ComfyUI checkout when building the EXISTING ComfyUI app:
COMFYUI_REVISION=5bbdf8a76678e2c7cfb519a49a9c3a7137fd6280
```

The frontend's `.env.local` needs `AMBIENT_BACKEND_URL` pointing to the **Ambient API**, plus `MODAL_PROXY_KEY`/`MODAL_PROXY_SECRET`. It accesses ComfyUI only through this job API. Neither asset-management UI nor a browser ComfyUI connection is required.

`GPU_PROFILE`, function timeout and scale-down behavior are reused from `comfyapp.py`. FastVideo has its own CUDA 13 image and uses one GPU, text encoder/VAE offloading, VSA-H3 with the Triton kernel and no FA4. FastVideo code is fixed at `556ac7088e7b4750806d277d31e0db6cd25a5238`. Dependencies follow that revision's `[fasth3]` installation recipe. GPU memory suitability and dependency/image build must still be validated on the chosen profile; the upstream speed claim uses four B200s and is not a promise for this single-GPU configuration.

## Explicit provisioning (incurs cloud usage; not automatic)

When ready to use Modal again:

1. Prepare missing H3 files using the existing model saver:
   `./scripts/modal.sh run scripts/prepare_ambient_h3.py`.
   This uses `Comfy-Org/MiniMax-H3` at `a98869194787969724c7425d95d0ed73ce9202af` and its original model directories.
2. Build/deploy the existing ComfyUI app if it needs the optional pinned revision or ffmpeg:
   `./scripts/modal.sh deploy comfyapp.py`.
3. Prepare the separate FastVideo snapshot:
   `./scripts/modal.sh run ambient_app.py::prepare_fasth3`.
   All component files, tokenizers and configs go to `/models/ambient-fasth3/<revision>` on `comfy-model`. The model uses `modular_model_index.json`. Downloads finish and commit before readiness is recorded.
4. Deploy the new API/worker:
   `./scripts/modal.sh deploy ambient_app.py`.
5. Check the existing ComfyUI native node/model inventory:
   `./scripts/modal.sh run ambient_app.py::check_h3`.
   **This contacts/wakes ComfyUI**, but does not submit a generation. Capabilities report the inventory result, model snapshot readiness and separate `gpuValidated: false` status. API capability reads themselves use Dict only, so they never wake a GPU.
6. Run the explicit smoke script for native audio and three parent-linked clips:
   `python scripts/ambient_smoke.py --mode h3 --clips 3`, then `--mode fasth3 --clips 1`.
   Environment: `AMBIENT_BACKEND_URL`, `MODAL_PROXY_KEY`, `MODAL_PROXY_SECRET`. Downloads are saved locally; listen and visually inspect continuity before qualifying these pins.

Stopping the studio cancels its pending job and closes local resources. An already-running shared ComfyUI prompt is logically cancelled and drained rather than globally interrupted; it can still incur usage until it finishes. Scale-to-zero remains enabled.

## HTTP contract

All deployed routes require Modal Proxy Auth. The Next.js proxy supplies it server-side.

- `GET /capabilities`: supported modes, preparation status, resolutions, 124 frames / 24fps.
- `POST /images`: multipart field `image`, at most 12 MiB and 24 megapixels; returns `{id}`.
- `POST /jobs`: `{requestId,mode,prompt,sound,seed,resolution,imageId?,parentClipId?}`. UUID request IDs are atomic claims. Same content returns the existing job; different content with the same ID returns 409. `mode` is `h3` or `fasth3`, resolution `preview` or `quality`. FastH3 rejects all image/parent inputs. Sound is mandatory.
- `GET /jobs/:id`: `queued/running/completed/failed/cancelled`, stage/error, completed clip metadata with actual dimensions/duration/frame count and `hasAudio`.
- `DELETE /jobs/:id`: marks an independent cancellation tombstone. Late writes cannot un-cancel the job. The worker deletes only its own queued ComfyUI prompt; it never calls `/interrupt`.
- `GET /clips/:id`: durable H.264/AAC MP4, supports byte ranges. CPU-side Volume SDK materialization; no GPU wake-up.

ComfyUI upload uses `ambient/uploads`, raw output uses `ambient/raw`. Final MP4s are `comfy-outputs/ambient/clips/<id>.mp4`; exact decoded final frames are `comfy-inputs/ambient/frames/<id>.png`. Uploaded anchors are `comfy-inputs/ambient/images/<id>.png`. Both final artifacts commit before `completed` is published. ffmpeg requires an audio stream; missing audio or A/V duration mismatch is a failed job. A 24-hour retention task removes only Ambient-owned files and terminal job records, never other ComfyUI assets or models.

A backend control-only WebSocket keeps ComfyUI alive and drains progress without forwarding binary previews to the browser. History is polled for completion. If the control connection or worker dies after prompt submission, the job fails with an uncertain-upstream-result message rather than submitting a second GPU prompt. Dispatch crashes are never retried by re-spawning the same ID. HTTP retry uses the original ID.

## Local verification

```sh
uv sync --extra ambient-test
uv run python -m unittest discover -s tests
```

Tests cover idempotency/conflicts, dispatch failure/worker timeout, cancellation vs late completion, input capability restrictions, multipart validation, range delivery, audio-required encoding/final-frame extraction, and a local aiohttp ComfyUI double proving that cancellation touches only the owned prompt.

References: [ComfyUI H3 native workflows](https://docs.comfy.org/tutorials/video/minimax/minimax-h3), [FastH3 VSA Preview v1](https://huggingface.co/FastVideo/FastVideo-FastH3-4-step-Preview-v1-VSA-DataFree), [FastVideo pinned recipe](https://github.com/hao-ai-lab/FastVideo/blob/556ac7088e7b4750806d277d31e0db6cd25a5238/examples/inference/basic/basic_fasth3.py).
